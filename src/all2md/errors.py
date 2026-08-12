from __future__ import annotations


class All2MDError(Exception):
    """Base class for expected service failures."""


class UploadTooLargeError(All2MDError):
    pass


class EmptyUploadError(All2MDError):
    pass


class QueueFullError(All2MDError):
    pass


class IdempotencyConflictError(All2MDError):
    pass


class ConversionBusyError(All2MDError):
    pass


class ConversionTimeoutError(All2MDError):
    pass


class ConversionProcessError(All2MDError):
    pass


class ConversionCancelledError(All2MDError):
    pass


class ResultTooLargeError(All2MDError):
    pass
