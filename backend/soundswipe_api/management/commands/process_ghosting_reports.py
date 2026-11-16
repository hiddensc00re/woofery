from django.core.management.base import BaseCommand
from django.utils import timezone

from soundswipe_api.feedback_payouts import process_ghosting_report
from soundswipe_api.models import GhostingReport


class Command(BaseCommand):
    help = 'Process expired ghosting reports and record any payouts due.'

    def handle(self, *args, **options):
        now = timezone.now()
        report_ids = GhostingReport.objects.filter(
            deadline__lte=now, processed_at__isnull=True,
        ).values_list('pk', flat=True)
        payouts = sum(process_ghosting_report(report_id, now) for report_id in list(report_ids))
        self.stdout.write(f'Recorded {payouts} new ghosting payouts.')
