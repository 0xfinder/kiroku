class KirokuError(Exception):
    """An expected failure the user fixes (bad config, database down, edited migration).

    The CLI prints the message as one line and exits 2. Anything else is a bug and keeps
    its traceback.
    """
