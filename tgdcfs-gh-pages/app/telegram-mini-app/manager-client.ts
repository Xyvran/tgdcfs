export interface Task {
  id: string;
  type: "upload" | "download";
  path: string;
  filename: string;
  status: "pending" | "in_progress" | "completed" | "failed";
  progress: number; // 0.0 to 1.0
  size_total?: number;
  size_processed?: number;
  error_message?: string;
  created_at?: string;
  updated_at?: string;
  speed_bytes_per_sec?: number;
}

export interface ChannelMessage {
  id: number;
  file_size: number;
  caption: string;
  mime_type: string;
}

export interface ReplicationItem {
  path: string;
  queued_at: number;
  attempts: number;
  last_error: string | null;
}

// File system name -> files waiting for background replication.
export type ReplicationQueue = { [filesystem: string]: ReplicationItem[] };

export interface CacheStats {
  enabled: boolean;
  directory?: string;
  entries?: number;
  complete_entries?: number;
  used_bytes?: number;
  max_size_bytes?: number;
  max_files?: number;
  pinned_entries?: number;
  pinned_bytes?: number;
  writing_entries?: number;
  hits?: number;
  misses?: number;
  disk_free_bytes?: number | null;
  min_free_bytes?: number;
  target_bytes?: number | null;
  max_age_hours?: number;
  last_sweep?: number | null;
  per_filesystem?: {
    [filesystem: string]: { entries: number; bytes: number; pinned: number };
  };
}

export interface FilesystemInfo {
  primary: string;
  mirrors: string[];
  mode: string;
  sync: string;
  strict: boolean;
  read_preference: string[];
  read_parallel?: boolean;
  read_sources?: string[];
  write_ack?: string;
  metadata: string;
  primary_dead: boolean;
}

export default class ManagerClient {
  private baseUrl: string;
  private jwtToken: string;

  constructor(baseUrl: string, jwtToken: string) {
    this.baseUrl = baseUrl;
    this.jwtToken = jwtToken;
  }

  private async makeRequest<T>(
    endpoint: string,
    options?: RequestInit
  ): Promise<T> {
    const response = await fetch(`${this.baseUrl}${endpoint}`, {
      ...options,
      headers: {
        Authorization: `Bearer ${this.jwtToken}`,
        ...options?.headers,
      },
    });

    if (!response.ok) {
      throw new Error(await response.text());
    }

    return response.json();
  }

  async getAllTasks(): Promise<Task[]> {
    return this.makeRequest<Task[]>("/tasks");
  }

  async getTasksUnderPath(path: string): Promise<Task[]> {
    const encodedPath = encodeURIComponent(path);
    return this.makeRequest<Task[]>(`/tasks?path=${encodedPath}`);
  }

  async getTask(taskId: string): Promise<Task> {
    return this.makeRequest<Task>(`/tasks/${taskId}`);
  }

  async deleteTask(taskId: string): Promise<void> {
    await this.makeRequest<void>(`/tasks/${taskId}`, {
      method: "DELETE",
    });
  }

  async getMessage(
    channelId: number,
    messageId: number
  ): Promise<ChannelMessage> {
    return this.makeRequest<ChannelMessage>(
      `/message/${channelId}/${messageId}`
    );
  }

  async importTelegramMessage(
    channelId: number,
    messageId: number,
    directory: string,
    asName: string
  ) {
    return await this.makeRequest<void>(`/import`, {
      headers: {
        "Content-Type": "application/json",
      },
      method: "POST",
      body: JSON.stringify({
        channel_id: channelId,
        message_id: messageId,
        directory,
        name: asName,
      }),
    });
  }

  async getFilesystems(): Promise<{ [name: string]: FilesystemInfo }> {
    return this.makeRequest<{ [name: string]: FilesystemInfo }>("/filesystems");
  }

  async getReplicationQueue(): Promise<ReplicationQueue> {
    return this.makeRequest<ReplicationQueue>("/replication/queue");
  }

  async getCacheStats(): Promise<CacheStats> {
    return this.makeRequest<CacheStats>("/cache");
  }

  async evictCache(): Promise<number> {
    const result = await this.makeRequest<{ evicted: number }>("/cache/evict", {
      method: "POST",
    });
    return result.evicted;
  }

  async retryReplication(filesystem?: string): Promise<void> {
    const query = filesystem
      ? `?filesystem=${encodeURIComponent(filesystem)}`
      : "";
    await this.makeRequest<{ message: string }>(`/replication/retry${query}`, {
      method: "POST",
    });
  }

  async startBackfill(filesystem: string, verify = false): Promise<string> {
    const result = await this.makeRequest<{ task_id: string }>(
      `/redundancy/backfill/${encodeURIComponent(filesystem)}?verify=${verify}`,
      { method: "POST" }
    );
    return result.task_id;
  }

  formatFileSize(bytes?: number): string {
    if (!bytes || bytes === 0) return "0 Bytes";
    const k = 1024;
    const sizes = ["Bytes", "KB", "MB", "GB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + " " + sizes[i];
  }

  formatProgress(progress: number): string {
    return `${Math.round(progress * 100)}%`;
  }

  formatSpeed(bytesPerSec?: number): string {
    if (!bytesPerSec || bytesPerSec === 0) return "";

    const k = 1024;
    const sizes = ["B/s", "KB/s", "MB/s", "GB/s"];
    const i = Math.floor(Math.log(bytesPerSec) / Math.log(k));
    const value = parseFloat((bytesPerSec / Math.pow(k, i)).toFixed(1));

    return `${value} ${sizes[i]}`;
  }

  formatDate(dateString?: string): string {
    if (!dateString) return "";
    try {
      const date = new Date(dateString);
      return date.toLocaleString();
    } catch {
      return dateString;
    }
  }

  getTaskStatusColor(status: string): string {
    switch (status) {
      case "pending":
        return "#f59e0b"; // amber
      case "in_progress":
        return "#3b82f6"; // blue
      case "completed":
        return "#10b981"; // green
      case "failed":
        return "#ef4444"; // red
      default:
        return "#6b7280"; // gray
    }
  }

  getTaskTypeIcon(type: string): string {
    switch (type) {
      case "upload":
        return "↑";
      case "download":
        return "↓";
      default:
        return "?";
    }
  }
}
