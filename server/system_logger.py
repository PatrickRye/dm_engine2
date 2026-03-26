import logging
import json
import os
from datetime import datetime, timezone

# ----------------------------------------------------------------------
# Console logging configuration
# LOG_LEVEL env var controls console output (default: WARNING).
# Valid values: DEBUG, INFO, WARNING, ERROR, CRITICAL
# Set to DEBUG to see all logs, WARNING to see only warnings/errors.
# ----------------------------------------------------------------------
_CONSOLE_LEVEL = os.environ.get("LOG_LEVEL", "WARNING").upper()
_CONSOLE_LEVEL_VAL = getattr(logging, _CONSOLE_LEVEL, logging.WARNING)


class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec='microseconds'),
            "level": record.levelname,
            "module": record.module,
            "process_id": record.process,
            "thread_id": record.thread,
            "agent_id": getattr(record, 'agent_id', 'SYSTEM'),
            "client_id": getattr(record, 'context', {}).get("client_id", "SYSTEM"),
            "character": getattr(record, 'context', {}).get("character", "SYSTEM"),
            "campaign_name": getattr(record, 'context', {}).get("vault_path", "UNKNOWN"),
            "message": record.getMessage(),
            "context": getattr(record, 'context', {}),
            # Canonical stack trace field name (also written as "exception" for backward compat)
            "stack_trace": None,
        }
        if record.exc_info:
            tb = self.formatException(record.exc_info)
            log_entry["stack_trace"] = tb
            log_entry["exception"] = tb   # backward-compat alias
        return json.dumps(log_entry)


os.makedirs("logs/active", exist_ok=True)
os.makedirs("logs/qa_audits", exist_ok=True)

# ----------------------------------------------------------------------
# DM_Engine logger: file only (JSONL) + optional console handler
# ----------------------------------------------------------------------
logger = logging.getLogger("DM_Engine")
if not logger.handlers:
    # File handler — always writes full DEBUG to JSONL
    file_handler = logging.FileHandler(f"logs/active/system_run_{os.getpid()}.jsonl")
    file_handler.setFormatter(JSONFormatter())
    file_handler.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)

    # Console handler — respects LOG_LEVEL (default: WARNING)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))
    console_handler.setLevel(_CONSOLE_LEVEL_VAL)
    logger.addHandler(console_handler)

    logger.setLevel(logging.DEBUG)  # actual filtering done per-handler

# ----------------------------------------------------------------------
# QA_Audits logger: file only (JSONL) + optional console handler
# ----------------------------------------------------------------------
qa_logger = logging.getLogger("QA_Audits")
if not qa_logger.handlers:
    qa_handler = logging.FileHandler(f"logs/qa_audits/qa_run_{os.getpid()}.jsonl")
    qa_handler.setFormatter(JSONFormatter())
    qa_handler.setLevel(logging.DEBUG)
    qa_logger.addHandler(qa_handler)

    # Console handler — respects LOG_LEVEL (default: WARNING)
    qa_console_handler = logging.StreamHandler()
    qa_console_handler.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))
    qa_console_handler.setLevel(_CONSOLE_LEVEL_VAL)
    qa_logger.addHandler(qa_console_handler)

    qa_logger.setLevel(logging.DEBUG)  # actual filtering done per-handler