"""Coordinate mutually exclusive lab-changing operations."""

import threading


_LOCK = threading.Lock()
_CURRENT_OPERATION = None


def acquire_operation(operation):
    """Acquire the global lab-operation slot without blocking."""
    global _CURRENT_OPERATION

    with _LOCK:
        if _CURRENT_OPERATION is not None:
            return {
                "acquired": False,
                "operation": _CURRENT_OPERATION,
            }

        _CURRENT_OPERATION = operation

        return {
            "acquired": True,
            "operation": operation,
        }


def release_operation(operation):
    """Release the slot only when owned by the caller."""
    global _CURRENT_OPERATION

    with _LOCK:
        if _CURRENT_OPERATION == operation:
            _CURRENT_OPERATION = None
            return True

        return False


def get_operation():
    """Return the currently active lab-changing operation."""
    with _LOCK:
        return _CURRENT_OPERATION


def busy_response(current_operation):
    """Return a safe response for a competing operation."""
    return {
        "status": "busy",
        "operation": current_operation,
        "message": "Another lab operation is currently running.",
    }
