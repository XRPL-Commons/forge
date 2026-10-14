"""Worker error classification shared by the controllers."""


def classify_error(text: str) -> str:
    """Use only CLI error channels, never successful model prose, for health."""
    lowered = text.lower()
    if any(s in lowered for s in ("bwrap:", "bubblewrap is required", "no space left on device",
                                  "cannot allocate memory", "sandbox could not initialize")):
        return "failed_infrastructure"
    if any(s in lowered for s in ("permission denied", "approval required", "sandbox denied")):
        return "blocked_permission"
    if any(s in lowered for s in ("insufficient_quota", "credit balance", "usage limit",
                                  "out of credits", "exhausted credits")):
        return "blocked_quota"
    if any(s in lowered for s in ("authentication_error", "invalid_api_key", "not logged in",
                                  "please login", "please log in", "token expired", "unauthorized")):
        return "blocked_auth"
    return "failed_retryable"
