import asyncio
import datetime
import os
from typing import Dict, List, Optional
from uuid import uuid4

from .models import Task, TaskStatus, TaskType

# Finished tasks are only kept so the manager can show recent transfers.
# Without a bound every upload and backfilled file would stay in memory
# until the process restarts, so they are pruned whenever a task is added:
# by age, and by count so a large backfill cannot pile up within a day.
FINISHED_TASK_MAX_AGE_HOURS = 24
MAX_FINISHED_TASKS = 500

_FINISHED = (TaskStatus.COMPLETED, TaskStatus.FAILED)


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class TaskStore:
    def __init__(self):
        self._tasks: Dict[str, Task] = {}
        self._lock = asyncio.Lock()

    async def add_task(
        self,
        task_type: TaskType,
        path: str,
        filename: str,
        size_total: Optional[int] = None,
    ) -> str:
        async with self._lock:
            self._prune_finished_locked()

            task_id = str(uuid4())
            now = utcnow().isoformat()

            task = Task(
                id=task_id,
                type=task_type,
                path=path,
                filename=filename,
                status=TaskStatus.PENDING,
                progress=0.0,
                size_total=size_total,
                size_processed=0,
                created_at=now,
                updated_at=now,
            )

            self._tasks[task_id] = task
            return task_id

    async def update_task_progress(
        self,
        task_id: str,
        size_delta: Optional[int] = None,
        status: Optional[TaskStatus] = None,
        error_message: Optional[str] = None,
    ) -> bool:
        async with self._lock:
            if task_id not in self._tasks:
                return False

            task = self._tasks[task_id]

            # Calculate speed if size_processed is being updated
            if size_delta is not None and task.size_processed is not None:
                now = utcnow()
                if task.updated_at:
                    try:
                        last_update = datetime.datetime.fromisoformat(task.updated_at)
                        time_diff = (now - last_update).total_seconds()

                        if time_diff > 0:
                            task.speed_bytes_per_sec = size_delta / time_diff
                    except (ValueError, TypeError):
                        # If timestamp parsing fails, don't calculate speed
                        pass

            task.updated_at = utcnow().isoformat()

            if task.size_processed is not None and size_delta is not None:
                task.size_processed += size_delta
                # Auto-calculate progress based on size_processed and size_total
                if task.size_total and task.size_total > 0:
                    task.progress = min(1.0, task.size_processed / task.size_total)
                else:
                    # If no size_total, use status to determine progress
                    if status == TaskStatus.COMPLETED:
                        task.progress = 1.0
                    elif status == TaskStatus.FAILED:
                        task.progress = 0.0
                    # Otherwise keep current progress

            if status is not None:
                task.status = status
                # Ensure progress is consistent with status
                if status == TaskStatus.COMPLETED:
                    task.progress = 1.0

            if error_message is not None:
                task.error_message = error_message

            return True

    async def get_task(self, task_id: str) -> Optional[Task]:
        async with self._lock:
            return self._tasks.get(task_id)

    async def get_all_tasks(self) -> List[Task]:
        async with self._lock:
            return list(self._tasks.values())

    async def get_tasks_under_path(self, path: str) -> List[Task]:
        async with self._lock:
            normalized_path = path.rstrip("/")
            if normalized_path == "":
                normalized_path = "/"

            result = []
            for task in self._tasks.values():
                task_dir = os.path.dirname(task.path).rstrip("/")
                if task_dir == "" and normalized_path == "/":
                    result.append(task)
                elif task_dir == normalized_path:
                    result.append(task)

            return result

    async def remove_task(self, task_id: str) -> bool:
        async with self._lock:
            if task_id in self._tasks:
                del self._tasks[task_id]
                return True
            return False

    async def cleanup_completed_tasks(
        self, max_age_hours: int = FINISHED_TASK_MAX_AGE_HOURS
    ) -> int:
        async with self._lock:
            return self._remove_finished_older_than_locked(max_age_hours)

    def _remove_finished_older_than_locked(self, max_age_hours: float) -> int:
        cutoff_time = datetime.datetime.now(datetime.UTC).timestamp() - (
            max_age_hours * 3600
        )
        to_remove = []

        for task_id, task in self._tasks.items():
            if task.status in _FINISHED:
                if task.updated_at:
                    task_time = datetime.datetime.fromisoformat(
                        task.updated_at
                    ).timestamp()
                    if task_time < cutoff_time:
                        to_remove.append(task_id)

        for task_id in to_remove:
            del self._tasks[task_id]

        return len(to_remove)

    def _prune_finished_locked(self) -> int:
        """Drop finished tasks past their age, then the oldest beyond the cap.

        Tasks that are still pending or running are never touched: a task
        missing from the store reads as cancelled to its ``TaskTracker``.
        """
        removed = self._remove_finished_older_than_locked(FINISHED_TASK_MAX_AGE_HOURS)

        finished = [
            (task.updated_at or "", task_id)
            for task_id, task in self._tasks.items()
            if task.status in _FINISHED
        ]
        excess = len(finished) - MAX_FINISHED_TASKS
        if excess > 0:
            # ISO timestamps in UTC sort chronologically as strings; a task
            # without one counts as the oldest.
            finished.sort()
            for _, task_id in finished[:excess]:
                del self._tasks[task_id]
            removed += excess

        return removed


# Global task store instance
task_store = TaskStore()
