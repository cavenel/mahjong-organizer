"""A 500 must reach the container log.

Django's DEFAULT_LOGGING survives (disable_existing_loggers is False), but its
console handler carries RequireDebugTrue and its mail_admins handler needs
ADMINS/EMAIL_*, none of which this project sets. So without an explicit `django`
logger, an unhandled exception under DEBUG=False wrote nothing to `docker logs web`.

These inspect the resolved handler chain rather than capturing output, so they don't
depend on how pytest wraps stderr.
"""
import logging
import logging.config

import pytest
from django.conf import settings
from django.utils.log import DEFAULT_LOGGING


def _handlers_reaching(name):
    """Handlers a record on `name` would be offered, following propagation.

    The root logger is deliberately excluded: under pytest it carries the runner's own
    capture handlers (LogCaptureHandler and friends), which would make every check
    pass regardless of the project's config. Excluding it is only sound because the
    project declares no root handler — asserted by
    ``test_the_project_declares_no_root_handler`` below, so this stops being sound the
    moment that changes.
    """
    found, lg = [], logging.getLogger(name)
    while lg is not None and lg.name != 'root':
        found.extend(lg.handlers)
        if not lg.propagate:
            break
        lg = lg.parent
    return found


def _is_debug_only(handler):
    """True if this handler is gated on DEBUG — i.e. silent in production."""
    return any(type(f).__name__ == 'RequireDebugTrue' for f in handler.filters)


def _writes_to_the_container_log(handler):
    """True if this handler puts the record where `docker logs web` shows it.

    Deliberately narrower than "not DEBUG-gated": Django's DEFAULT_LOGGING also
    attaches mail_admins, which carries RequireDebugFalse and so *looks* like a
    production handler — but it needs ADMINS and EMAIL_* to deliver anything, and this
    project sets neither. Only a stream handler actually reaches the container log.
    """
    return isinstance(handler, logging.StreamHandler) and not _is_debug_only(handler)


@pytest.fixture(autouse=True)
def _restore_project_logging():
    """These tests apply configs globally, so put the project's back afterwards."""
    yield
    logging.config.dictConfig(settings.LOGGING)


def _reaches_a_production_handler(name):
    return any(_writes_to_the_container_log(h) for h in _handlers_reaching(name))


class TestUnhandledExceptionsAreLogged:
    def test_django_request_reaches_a_handler_that_works_in_production(self):
        logging.config.dictConfig(settings.LOGGING)
        assert _reaches_a_production_handler('django.request'), (
            'a 500 would be written nowhere under DEBUG=False')

    def test_the_apps_own_logger_does_too(self):
        logging.config.dictConfig(settings.LOGGING)
        assert _reaches_a_production_handler('mahj.views.admin_views')

    def test_without_the_django_logger_nothing_reaches_one(self):
        """The discriminator. Rebuild the project's config minus the `django` entry —
        which is what was there before — and the chain falls back to Django's own
        DEBUG-gated console handler."""
        # Django applies DEFAULT_LOGGING first, then the project's dict. dictConfig
        # with disable_existing_loggers=False leaves handlers it doesn't mention in
        # place, so the project's console handler has to be detached explicitly to
        # reconstruct the pre-fix state.
        logging.config.dictConfig(DEFAULT_LOGGING)
        for name in ('django', 'mahj', 'mahj.views.scan'):
            logging.getLogger(name).handlers = [
                h for h in logging.getLogger(name).handlers
                if not _writes_to_the_container_log(h)]
        assert not _reaches_a_production_handler('django.request'), (
            "if this fails the test no longer proves the project's config is what "
            "makes 500s visible")


def test_the_project_configures_the_django_logger():
    """Guard against the entry being dropped in a later edit."""
    assert 'django' in settings.LOGGING['loggers']
    assert settings.LOGGING['loggers']['django']['handlers'] == ['console']


def test_the_project_declares_no_root_handler():
    """What licenses _handlers_reaching to skip the root logger. If a root handler is
    ever added, that helper must count it instead of assuming named loggers are the
    only route."""
    assert 'root' not in settings.LOGGING
    assert not settings.LOGGING.get('handlers', {}).get('root')
