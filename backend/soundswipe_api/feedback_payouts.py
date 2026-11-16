from django.db import transaction

from .models import ChatRoom, Feedback, FeedbackPayout, GhostingReport


def record_feedback_payout(feedback):
    if feedback.grammar_review != Feedback.GrammarReview.PASSED:
        return False
    _, created = FeedbackPayout.objects.get_or_create(
        feedback=feedback, defaults={'amount_cents': 100, 'currency': 'EUR'},
    )
    return created


@transaction.atomic
def process_ghosting_report(report_id, now):
    report = GhostingReport.objects.select_related('feedback__chat_message').get(pk=report_id)
    feedback = report.feedback
    message = feedback.chat_message
    room = ChatRoom.objects.select_for_update().filter(pk=message.room_id if message else None).first()
    report = GhostingReport.objects.select_for_update().get(pk=report_id)
    if report.processed_at is not None or report.deadline > now:
        return False

    # Evaluate the chat at the deadline even if the command runs after a late reply or closure.
    payout_created = False
    if (
        room is not None
        and not room.track.feedback_declined
        and (room.closed_at is None or room.closed_at >= report.deadline)
        and not room.messages.filter(
            sender_id=room.track.uploader_id, timestamp__lt=report.deadline,
        ).exists()
    ):
        feedback.refresh_from_db(fields=['grammar_review'])
        payout_created = record_feedback_payout(feedback)

    report.processed_at = now
    report.save(update_fields=['processed_at'])
    return payout_created
