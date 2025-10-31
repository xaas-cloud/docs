"""Trigger document indexation using celery task."""

from logging import getLogger

from django.conf import settings
from django.core.cache import cache

from django_redis.cache import RedisCache

from core import models
from core.services.search_indexers import (
    get_batch_accesses_by_users_and_teams,
    get_document_indexer,
)

from impress.celery_app import app

logger = getLogger(__file__)


def indexer_throttle_acquire(document_id, timeout=0, atomic=True):
    """
    Enable the task throttle flag for a delay.
    Uses redis locks if available to ensure atomic changes
    """
    key = f"doc-indexer-throttle-{document_id}"

    if isinstance(cache, RedisCache) and atomic:
        with cache.locks(key):
            return indexer_throttle_acquire(document_id, timeout, atomic=False)

    # Use add() here :
    #   - set the flag and returns true if not exist
    #   - do nothing and return false if exist
    return cache.add(key, 1, timeout=timeout)


@app.task
def document_indexer_task(document_id):
    """Celery Task : Sends indexation query for a document."""
    indexer = get_document_indexer()

    if indexer is None:
        return

    try:
        doc = models.Document.objects.get(pk=document_id)
    except models.Document.DoesNotExist:
        # Skip the task if the document does not exist.
        return

    accesses = get_batch_accesses_by_users_and_teams((doc.path,))

    data = indexer.serialize_document(document=doc, accesses=accesses)

    logger.info("Start document %s indexation", document_id)
    indexer.push(data)


def trigger_document_indexer(document):
    """
    Trigger indexation task with debounce a delay set by the SEARCH_INDEXER_COUNTDOWN setting.

    Args:
        document (Document): The document instance.
    """
    countdown = settings.SEARCH_INDEXER_COUNTDOWN

    # DO NOT create a task if indexation if disabled
    if not settings.SEARCH_INDEXER_CLASS:
        return

    # Each time this method is called during a countdown, we increment the
    # counter and each task decrease it, so the index be run only once.
    if indexer_throttle_acquire(document.pk, timeout=countdown):
        logger.info(
            "Add task for document %s indexation in %.2f seconds",
            document.pk,
            countdown,
        )

        document_indexer_task.apply_async(args=[document.pk])
    else:
        logger.info("Skip task for document %s indexation", document.pk)


def batch_indexer_throttle_acquire(timeout: int = 0, atomic: bool = True):
    """
    Enable the task throttle flag for a delay.
    Uses redis locks if available to ensure atomic changes
    """
    key = "document-batch-indexer-throttle"

    # Redis is used as cache database (not in tests). Use the lock feature here
    # to ensure atomicity of changes to the throttle flag.
    if isinstance(cache, RedisCache) and atomic:
        with cache.locks(key):
            return batch_indexer_throttle_acquire(timeout, atomic=False)

    # Use add() here :
    #   - set the flag and returns true if not exist
    #   - do nothing and return false if exist
    return cache.add(key, 1, timeout=timeout)


@app.task
def batch_document_indexer_task(timestamp):
    """Celery Task : Sends indexation query for a batch of documents."""
    indexer = get_document_indexer()

    if indexer:
        queryset = models.Document.objects.filter(updated_at__gte=timestamp)

        count = indexer.index(queryset)
        logger.info("Indexed %d documents", count)


def trigger_batch_document_indexer(item):
    """
    Trigger indexation task with debounce a delay set by the SEARCH_INDEXER_COUNTDOWN setting.

    Args:
        document (Document): The document instance.
    """
    countdown = int(settings.SEARCH_INDEXER_COUNTDOWN)

    # DO NOT create a task if indexation if disabled
    if not settings.SEARCH_INDEXER_CLASS:
        return

    # Ignore triggres from workspace items created along users
    if item.main_workspace:
        return

    if countdown > 0:
        # Each time this method is called during a countdown, we increment the
        # counter and each task decrease it, so the index be run only once.
        if batch_indexer_throttle_acquire(timeout=countdown):
            logger.info(
                "Add task for batch document indexation from updated_at=%s in %d seconds",
                item.updated_at.isoformat(),
                countdown,
            )

            batch_document_indexer_task.apply_async(
                args=[item.updated_at], countdown=countdown
            )
        else:
            logger.info("Skip task for batch document %s indexation", item.pk)
    else:
        document_indexer_task.apply(args=[item.pk])
