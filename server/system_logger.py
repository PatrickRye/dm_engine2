import logging
import json
import os
from datetime import datetime, timezone

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
            "context": getattr(record, 'context', {})
        }
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry)

os.makedirs("logs/active", exist_ok=True)
os.makedirs("logs/qa_audits", exist_ok=True)

# Setup Logger
logger = logging.getLogger("DM_Engine")
if not logger.handlers:
    # Embed the Process ID to ensure true instance-safety across multiple workers
    handler = logging.FileHandler(f"logs/active/system_run_{os.getpid()}.jsonl") # JSON Lines format
    handler.setFormatter(JSONFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

qa_logger = logging.getLogger("QA_Audits")
if not qa_logger.handlers:
    qa_handler = logging.FileHandler(f"logs/qa_audits/qa_run_{os.getpid()}.jsonl")
    qa_handler.setFormatter(JSONFormatter())
    qa_logger.addHandler(qa_handler)
    qa_logger.setLevel(logging.DEBUG)