"""Shared logging setup for PenguinCAM.

One place configures logging (INFO level, message-only format, to stderr for Vercel /
serverless) and provides the ``log()`` helper that used to be copy-pasted, verbatim, into
every module. Import ``log`` from here instead of redefining it.
"""
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format='%(message)s',
    stream=sys.stderr,
    force=True,
)

_logger = logging.getLogger('penguincam')


def log(*args, **kwargs):
    """Space-join args and log them at INFO (to stderr). Vercel/serverless-friendly.

    The message-only format means the logger name is irrelevant to the output, so a single
    shared logger is byte-for-byte equivalent to the previous per-module loggers.
    """
    _logger.info(' '.join(str(arg) for arg in args))
