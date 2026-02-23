import os
import json
import logging
import shutil
import tempfile
import time

logger = logging.getLogger(__name__)


class StateManager:
    def __init__(self, file_path: str, max_size_mb: int = 5):
        self.file_path = file_path
        self.max_size_bytes = max_size_mb * 1024 * 1024
        dir_name = os.path.dirname(self.file_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)

    def load(self):
        if not os.path.exists(self.file_path):
            return {}

        try:
            size = os.path.getsize(self.file_path)
            if size > self.max_size_bytes:
                logger.error(f"type=state_file_too_large | size_bytes={size}")
                return {}

            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except FileNotFoundError:
                logger.warning("type=state_missing_during_read")
                return {}

            if not isinstance(data, dict):
                logger.error("type=state_invalid_format | format=not_dict")
                return {}

            return data

        except json.JSONDecodeError:
            logger.error("type=state_corrupted_json | action=creating_backup")
            self._backup_corrupted()
            return {}

        except Exception as e:
            logger.error(f"type=state_load_error | error={e}")
            return {}

    def save(self, state: dict):
        if not isinstance(state, dict):
            logger.error("type=state_save_invalid_type | format=not_dict")
            return

        try:
            serialized = json.dumps(state, ensure_ascii=False, indent=2)
            if len(serialized.encode("utf-8")) > self.max_size_bytes:
                logger.error("type=state_save_too_large | action=aborting")
                return

            dir_name = os.path.dirname(self.file_path)
            tmp_dir = dir_name if dir_name else None
            fd, tmp_path = tempfile.mkstemp(dir=tmp_dir)

            with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                tmp_file.write(serialized)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())

            os.replace(tmp_path, self.file_path)

            # Ensure directory metadata is flushed (crash-safe)
            if dir_name:
                dir_fd = os.open(dir_name, os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)

        except Exception as e:
            logger.error(f"type=state_save_error | error={e}")

    def _backup_corrupted(self):
        try:
            timestamp = int(time.time())
            backup_path = f"{self.file_path}.corrupted.{timestamp}"
            shutil.move(self.file_path, backup_path)
            logger.warning(f"type=state_backup_created | path={backup_path}")

            # Keep only last 3 corrupted backups
            base = os.path.basename(self.file_path)
            dir_name = os.path.dirname(self.file_path)
            list_dir = dir_name if dir_name else "."
            backups = sorted(
                [f for f in os.listdir(list_dir) if f.startswith(base + ".corrupted.")],
                reverse=True,
            )
            for old in backups[3:]:
                try:
                    os.remove(os.path.join(list_dir, old))
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"type=state_backup_failed | error={e}")