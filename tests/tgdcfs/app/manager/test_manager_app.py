import pytest
from fastapi.testclient import TestClient
from tgdcfs.app.manager.app import create_manager_app
from tgdcfs.backends.base import StoreCapabilities
from tgdcfs.config import Config
from tgdcfs.core.client import Client


class TestManagerApp:
    @pytest.fixture
    def mock_client(self, mocker):
        client = mocker.Mock(spec=Client)
        client.store = mocker.Mock()
        client.store.key = "tg:123456"
        client.store.get_messages = mocker.AsyncMock()
        client.store.caps = StoreCapabilities(
            max_part_bytes=2_000_000_000,
            max_text_chars=4096,
            supports_server_copy=True,
        )
        client.mirror_group = None
        return client

    @pytest.fixture
    def mock_clients(self, mock_client):
        return {"Test-Channel": mock_client}

    @pytest.fixture
    def mock_config(self):
        return Config.from_dict(
            {
                "telegram": {
                    "api_id": 1,
                    "api_hash": "h",
                    "bot": {"token": "t", "session_file": "bot.session"},
                    "private_file_channel": ["123456"],
                },
                "tgdcfs": {
                    "users": {},
                    "metadata": {
                        "123456": {"name": "Test-Channel", "type": "pinned_message"}
                    },
                    "jwt": {"secret": "s", "algorithm": "HS256", "life": 3600},
                    "server": {"host": "0.0.0.0", "port": 8080},
                },
            }
        )

    def test_get_stores(self, mock_clients, mock_config):
        client = TestClient(create_manager_app(mock_clients, mock_config))

        response = client.get("/stores")

        assert response.status_code == 200
        assert response.json() == {
            "tg-123456": {
                "backend": "telegram",
                "channel": "123456",
                "key": "tg:123456",
                "primary_of": ["Test-Channel"],
                "mirror_of": [],
                "capabilities": {
                    "max_part_bytes": 2_000_000_000,
                    "max_text_chars": 4096,
                    "supports_server_copy": True,
                    "supports_edit_media": True,
                },
            }
        }

    def test_get_filesystems(self, mock_clients, mock_config):
        client = TestClient(create_manager_app(mock_clients, mock_config))

        response = client.get("/filesystems")

        assert response.status_code == 200
        assert response.json() == {
            "Test-Channel": {
                "primary": "tg-123456",
                "mirrors": [],
                "mode": "auto",
                "sync": "inline",
                "strict": False,
                "read_preference": [],
                "metadata": "pinned_message",
                "primary_dead": False,
            }
        }

    def test_replication_queue_endpoints(self, mock_clients, mock_config):
        from tgdcfs.core.replication import ReplicationQueue

        queue = ReplicationQueue(None)
        queue.enqueue("Test-Channel", "/a.bin")
        queue.failed("Test-Channel", "/a.bin", "boom")
        client = TestClient(
            create_manager_app(mock_clients, mock_config, replication=queue)
        )

        response = client.get("/replication/queue")
        assert response.status_code == 200
        [item] = response.json()["Test-Channel"]
        assert item["path"] == "/a.bin" and item["attempts"] == 1

        assert queue.ready("Test-Channel") == []
        assert (
            client.post("/replication/retry?filesystem=Test-Channel").status_code == 200
        )
        assert [i.path for i in queue.ready("Test-Channel")] == ["/a.bin"]
        assert client.post("/replication/retry?filesystem=nope").status_code == 404

    def test_replication_endpoints_without_a_queue(self, mock_clients, mock_config):
        client = TestClient(create_manager_app(mock_clients, mock_config))
        assert client.get("/replication/queue").json() == {}
        assert client.post("/replication/retry").status_code == 400

    def test_get_redundancy_without_mirrors(self, mock_clients, mock_config):
        client = TestClient(create_manager_app(mock_clients, mock_config))

        response = client.get("/redundancy")

        assert response.json() == {
            "Test-Channel": {"mirrors": [], "mode": None, "strict": False}
        }

    @pytest.fixture
    def manager_app(self, mock_client, mock_config):
        return create_manager_app({"channel": mock_client}, mock_config)

    def test_create_manager_app_returns_fastapi(self, mock_client, mock_config):
        app = create_manager_app({"channel": mock_client}, mock_config)
        assert hasattr(app, "get")
        assert hasattr(app, "post")
        assert hasattr(app, "delete")

    @pytest.mark.asyncio
    async def test_get_tasks_all(self, manager_app, mocker):
        mock_task_store = mocker.patch("tgdcfs.app.manager.app.task_store")
        mock_tasks = [
            mocker.Mock(to_dict=mocker.Mock(return_value={"id": "1", "path": "/test"}))
        ]
        mock_task_store.get_all_tasks = mocker.AsyncMock(return_value=mock_tasks)

        client = TestClient(manager_app)
        response = client.get("/tasks")

        assert response.status_code == 200
        assert response.json() == [{"id": "1", "path": "/test"}]
        mock_task_store.get_all_tasks.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_tasks_filtered_by_path(self, manager_app, mocker):
        mock_task_store = mocker.patch("tgdcfs.app.manager.app.task_store")
        mock_tasks = [
            mocker.Mock(
                to_dict=mocker.Mock(return_value={"id": "2", "path": "/specific"})
            )
        ]
        mock_task_store.get_tasks_under_path = mocker.AsyncMock(return_value=mock_tasks)

        client = TestClient(manager_app)
        response = client.get("/tasks?path=/specific")

        assert response.status_code == 200
        assert response.json() == [{"id": "2", "path": "/specific"}]
        mock_task_store.get_tasks_under_path.assert_called_once_with("/specific")

    @pytest.mark.asyncio
    async def test_get_task_found(self, manager_app, mocker):
        mock_task_store = mocker.patch("tgdcfs.app.manager.app.task_store")
        mock_task = mocker.Mock(
            to_dict=mocker.Mock(return_value={"id": "task123", "status": "running"})
        )
        mock_task_store.get_task = mocker.AsyncMock(return_value=mock_task)

        client = TestClient(manager_app)
        response = client.get("/tasks/task123")

        assert response.status_code == 200
        assert response.json() == {"id": "task123", "status": "running"}
        mock_task_store.get_task.assert_called_once_with("task123")

    @pytest.mark.asyncio
    async def test_get_task_not_found(self, manager_app, mocker):
        mock_task_store = mocker.patch("tgdcfs.app.manager.app.task_store")
        mock_task_store.get_task = mocker.AsyncMock(return_value=None)

        client = TestClient(manager_app)
        response = client.get("/tasks/nonexistent")

        assert response.status_code == 404
        assert "Task not found" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_delete_task_success(self, manager_app, mocker):
        mock_task_store = mocker.patch("tgdcfs.app.manager.app.task_store")
        mock_task_store.remove_task = mocker.AsyncMock(return_value=True)

        client = TestClient(manager_app)
        response = client.delete("/tasks/task123")

        assert response.status_code == 200
        assert response.json() == {"message": "Task deleted successfully"}
        mock_task_store.remove_task.assert_called_once_with("task123")

    @pytest.mark.asyncio
    async def test_delete_task_not_found(self, manager_app, mocker):
        mock_task_store = mocker.patch("tgdcfs.app.manager.app.task_store")
        mock_task_store.remove_task = mocker.AsyncMock(return_value=False)

        client = TestClient(manager_app)
        response = client.delete("/tasks/nonexistent")

        assert response.status_code == 404
        assert "Task not found" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_get_telegram_message_success(
        self, mock_client, mock_clients, mock_config, mocker
    ):
        # Mock the message response
        mock_message = mocker.Mock()
        mock_message.message_id = 456
        mock_message.document = mocker.Mock(size=1024, mime_type="text/plain")
        mock_message.text = "Test caption"
        mock_client.store.get_messages.return_value = [mock_message]

        app = create_manager_app(mock_clients, mock_config)
        client = TestClient(app)

        response = client.get("/message/123456/456")

        assert response.status_code == 200
        expected = {
            "id": 456,
            "file_size": 1024,
            "caption": "Test caption",
            "mime_type": "text/plain",
        }
        assert response.json() == expected
        mock_client.store.get_messages.assert_called_once_with([456])

    @pytest.mark.asyncio
    async def test_get_telegram_message_wrong_channel(self, mock_clients, mock_config):
        app = create_manager_app(mock_clients, mock_config)
        client = TestClient(app)

        response = client.get("/message/999999/456")

        assert response.status_code == 400
        assert "not in one of the configured file channels" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_get_telegram_message_not_found(
        self, mock_client, mock_clients, mock_config
    ):
        mock_client.store.get_messages.return_value = [None]

        app = create_manager_app(mock_clients, mock_config)
        client = TestClient(app)

        response = client.get("/message/123456/456")

        assert response.status_code == 404
        assert "Message 456 not found" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_get_telegram_message_no_document(
        self, mock_client, mock_clients, mock_config, mocker
    ):
        mock_message = mocker.Mock()
        mock_message.message_id = 456
        mock_message.document = None
        mock_client.store.get_messages.return_value = [mock_message]

        app = create_manager_app(mock_clients, mock_config)
        client = TestClient(app)

        response = client.get("/message/123456/456")

        assert response.status_code == 400
        assert "does not contain a document" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_import_telegram_message_success(
        self, mock_client, mock_clients, mock_config, mocker
    ):
        # Mock the message response
        mock_gfc = mocker.patch("tgdcfs.app.manager.app.gfc")
        mock_channel_cache = mock_gfc["Test-Channel"]
        mock_ops_class = mocker.patch("tgdcfs.app.manager.app.Ops")
        mock_message = mocker.Mock()
        mock_message.message_id = 456
        mock_message.document = mocker.Mock(size=1024, mime_type="text/plain")
        mock_message.text = "Test file"
        mock_client.store.get_messages.return_value = [mock_message]

        # Mock Ops
        mock_ops = mocker.Mock()
        mock_ops.import_from_existing_file_message = mocker.AsyncMock()
        mock_ops_class.return_value = mock_ops

        app = create_manager_app(mock_clients, mock_config)
        client = TestClient(app)

        payload = {
            "directory": "/Test-Channel/uploads",
            "name": "test.txt",
            "channel_id": 123456,
            "message_id": 456,
        }

        response = client.post("/import", json=payload)

        assert response.status_code == 200
        assert response.json() == {"message": "Document imported successfully"}
        mock_channel_cache.reset.assert_called_once_with("/uploads/")
        mock_ops.import_from_existing_file_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_import_telegram_message_wrong_channel(
        self, mock_clients, mock_config
    ):
        app = create_manager_app(mock_clients, mock_config)
        client = TestClient(app)

        payload = {
            "directory": "/uploads",
            "name": "test.txt",
            "channel_id": 999999,
            "message_id": 456,
        }

        response = client.post("/import", json=payload)

        assert response.status_code == 400
        assert "not in one of the configured file channels" in response.json()["detail"]
