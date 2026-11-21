from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.contrib.auth.models import User
from django.urls import reverse
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase
from .models import ChatMessage, ChatRoom, Feedback, FeedbackPayout, GhostingReport, Track, UserProfile

class SoundSwipeAPITests(APITestCase):

    def setUp(self):
        # Create test users
        self.creator = User.objects.create_user(username="test_creator", password="password123")
        self.creator_profile = UserProfile.objects.create(user=self.creator, role="creator")
        
        self.listener = User.objects.create_user(username="test_listener", password="password123")
        self.listener_profile = UserProfile.objects.create(user=self.listener, role="listener")
        
        # Create a track (simulate audio file in tests using a mock string or empty file)
        # For tests, we can skip actual file uploads if we test GET views, but we can mock values.
        self.track = Track.objects.create(
            uploader=self.creator,
            artist_name="Test Creator",
            track_name="Test Track",
            audio_file="tracks/test.wav",
            cover_image="covers/test.jpg",
            target_feedback="EQ feedback",
            max_listeners=5,
            is_active=True
        )

    def test_user_registration(self):
        url = reverse('register')
        data = {
            'username': 'new_user',
            'password': 'password123',
            'role': 'listener'
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['username'], 'new_user')
        self.assertEqual(response.data['role'], 'listener')

    def test_user_login(self):
        url = reverse('login')
        data = {
            'username': 'test_creator',
            'password': 'password123'
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['role'], 'creator')

    def test_get_swipe_deck(self):
        url = reverse('deck')
        # Call without auth headers -> should fail
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        
        response = self.client.get(url, **self.feedback_auth_headers(self.listener))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['track_name'], 'Test Track')

    def test_feedback_unlock_lifecycle(self):
        creator_headers = self.feedback_auth_headers(self.creator)
        listener_headers = self.feedback_auth_headers(self.listener)
        chat_headers = creator_headers
        summary_url = reverse('track_feedback', args=[self.track.id])
        listeners = [self.listener] + [
            User.objects.create_user(username=f'listener_{i}') for i in range(4)
        ]
        feedback_ids = []
        for number, listener in enumerate(listeners, 1):
            submission = self.client.post(reverse('feedback'), {
                'track_id': self.track.id, 'comment': f'Private comment {number}',
            }, format='json', **self.fixture_auth_headers(listener))
            self.assertEqual(submission.status_code, 201)
            feedback_ids.append(submission.data['feedback_id'])
            summary = self.client.get(summary_url, **creator_headers)
            self.assertEqual(summary.status_code, 200)
            self.assertEqual(summary.data['feedback_count'], number)
            self.assertTrue(summary.data['can_unlock'])
            self.assertEqual(summary.data['comments'], [
                {'id': feedback_id, 'unlocked': index == 0 and number > 1}
                for index, feedback_id in enumerate(feedback_ids)
            ])
            chats = self.client.get(reverse('chats'), **chat_headers).data
            self.assertEqual([room['last_message'] for room in chats],
                [] if number == 1 else ['Private comment 1'])
            self.assertEqual(ChatMessage.objects.count(), 0 if number == 1 else 1)
            if number != 1:
                continue

            unlock_url = reverse('unlock_feedback', args=[feedback_ids[0]])
            self.assertEqual(self.client.post(unlock_url, {'confirm': True},
                format='json', **listener_headers).status_code, 403)
            self.assertEqual(self.client.post(unlock_url, {'confirm': True},
                format='json', HTTP_X_USER_ID=str(self.creator.id)).status_code, 401)
            self.assertEqual(self.client.post(unlock_url, {'confirm': False},
                format='json', **creator_headers).status_code, 400)

            unlocked = self.client.post(unlock_url, {'confirm': True},
                format='json', **creator_headers)
            self.assertEqual(unlocked.status_code, 200)
            self.assertEqual(unlocked.data['payment'], {
                'simulated': True, 'amount_cents': 499, 'currency': 'EUR',
            })
            self.assertFalse(unlocked.data['already_unlocked'])
            self.track.refresh_from_db()
            self.assertTrue(self.track.is_active)

        self.track.refresh_from_db()
        self.assertFalse(self.track.is_active)
        self.assertEqual(self.client.post(summary_url, **creator_headers).status_code, 409)
        repeated = self.client.post(unlock_url, {'confirm': True},
            format='json', **creator_headers)
        self.assertEqual(repeated.status_code, 200)
        self.assertTrue(repeated.data['already_unlocked'])
        self.assertEqual(repeated.data['message_id'], unlocked.data['message_id'])

        chats = self.client.get(reverse('chats'), **chat_headers).data
        self.assertEqual([room['last_message'] for room in chats], ['Private comment 1'])
        messages = self.client.get(reverse('chat_messages',
            args=[unlocked.data['chat_room_id']]), **chat_headers).data
        self.assertEqual([message['content'] for message in messages], ['Private comment 1'])
        self.assertFalse(messages[0]['is_blockchain_written'])
        self.assertEqual(ChatMessage.objects.count(), 1)
        summary = self.client.get(summary_url, **creator_headers)
        self.assertEqual([item['unlocked'] for item in summary.data['comments']],
            [True, False, False, False, False])

    def test_only_owner_can_decline_full_locked_feedback(self):
        owner = self.feedback_auth_headers(self.creator)
        listener = self.feedback_auth_headers(self.listener)
        other_creator = User.objects.create_user('other_creator', password='password123')
        UserProfile.objects.create(user=other_creator, role='creator')
        other = self.feedback_auth_headers(other_creator)
        url = reverse('track_feedback', args=[self.track.id])

        self.assertEqual(self.client.post(url, **owner).status_code, 409)
        listeners = [self.listener] + [
            User.objects.create_user(f'decline_listener_{i}') for i in range(4)
        ]
        feedback_ids = []
        for index, account in enumerate(listeners):
            response = self.client.post(reverse('feedback'), {
                'track_id': self.track.id, 'comment': f'Locked advice {index}',
            }, format='json', **self.fixture_auth_headers(account))
            self.assertEqual(response.status_code, 201)
            feedback_ids.append(response.data['feedback_id'])

        self.assertEqual(self.client.post(url, **listener).status_code, 403)
        self.assertEqual(self.client.post(url, **other).status_code, 403)
        legacy_owner = {'HTTP_X_USER_ID': str(self.creator.id)}
        self.assertEqual(self.client.post(url, **legacy_owner).status_code, 401)
        self.assertTrue(self.client.get(url, **owner).data['can_decline'])
        self.assertEqual(self.client.post(url, **owner).status_code, 200)
        summary = self.client.get(url, **owner).data
        self.assertTrue(summary['declined'])
        self.assertEqual(summary['feedback_count'], 5)
        self.assertFalse(summary['can_decline'])
        self.assertFalse(summary['can_unlock'])
        self.assertEqual(summary['comments'], [
            {'id': feedback_id, 'unlocked': False} for feedback_id in feedback_ids
        ])

        for feedback_id in feedback_ids:
            unlock = self.client.post(reverse('unlock_feedback', args=[feedback_id]),
                {'confirm': True}, format='json', **owner)
            self.assertEqual(unlock.status_code, 409)
        self.assertEqual(self.client.get(reverse('chats'), **owner).data, [])
        self.assertFalse(ChatRoom.objects.exists())
        self.assertFalse(ChatMessage.objects.exists())
        self.assertFalse(FeedbackPayout.objects.exists())

        # Even a legacy room linked to declined feedback cannot qualify.
        feedback = Feedback.objects.get(pk=feedback_ids[0])
        feedback.grammar_review = Feedback.GrammarReview.PASSED
        room = ChatRoom.objects.create(
            track=self.track, uploader=self.creator, listener=self.listener,
        )
        feedback.chat_message = ChatMessage.objects.create(
            room=room, sender=self.listener, content=feedback.comment,
        )
        feedback.save(update_fields=['grammar_review', 'chat_message'])
        close = self.client.post(reverse('close_chat', args=[room.id]),
            {'reason': 'comment_not_helpful'}, format='json', **owner)
        self.assertEqual(close.status_code, 409)
        self.assertFalse(FeedbackPayout.objects.exists())

    def test_track_creator_closes_unlocked_chat_and_blocks_both_senders(self):
        feedback, room_id = self.make_unlocked_feedback(Feedback.GrammarReview.PASSED)
        close_url = reverse('close_chat', args=[room_id])
        messages_url = reverse('chat_messages', args=[room_id])
        close_data = {'reason': 'comment_not_helpful'}
        listener_headers = self.feedback_auth_headers(self.listener)
        other_creator = User.objects.create_user('unrelated_creator', password='password123')
        UserProfile.objects.create(user=other_creator, role='creator')
        for headers in (listener_headers, self.feedback_auth_headers(other_creator)):
            self.assertEqual(self.client.post(close_url, close_data, format='json',
                **headers).status_code, 403)
        self.assertEqual(self.client.post(close_url, close_data, format='json',
            HTTP_X_USER_ID=str(self.creator.id)).status_code, 401)
        self.assertFalse(FeedbackPayout.objects.exists())

        creator_headers = self.feedback_auth_headers(self.creator)
        invalid = self.client.post(close_url, {'reason': 'other'}, format='json', **creator_headers)
        self.assertEqual(invalid.status_code, 400)
        response = self.client.post(close_url, close_data, format='json', **creator_headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['close_reason'], 'comment_not_helpful')
        self.assertIsNotNone(response.data['closed_at'])
        self.assertTrue(response.data['payout_due'])
        self.assertEqual(response.data['payout'], {'amount_cents': 100, 'currency': 'EUR'})

        repeated = self.client.post(close_url, close_data, format='json', **creator_headers)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.data, response.data)
        self.assertEqual(FeedbackPayout.objects.count(), 1)
        payout = FeedbackPayout.objects.get()
        self.assertEqual(payout.feedback_id, feedback.id)
        self.assertEqual(payout.feedback.listener_id, self.listener.id)
        self.assertEqual((payout.amount_cents, payout.currency), (100, 'EUR'))

        for participant in (self.creator, self.listener):
            headers = self.feedback_auth_headers(participant)
            send = self.client.post(messages_url, {'content': 'Follow-up'},
                format='json', **headers)
            self.assertEqual(send.status_code, 409)
            history = self.client.get(messages_url, **headers)
            self.assertEqual(history.status_code, 200)
            self.assertEqual([message['content'] for message in history.data], [feedback.comment])
        self.assertEqual(ChatMessage.objects.filter(room_id=room_id).count(), 1)

    def test_failed_grammar_review_closes_chat_without_payout(self):
        _, room_id = self.make_unlocked_feedback(Feedback.GrammarReview.FAILED)
        close_url = reverse('close_chat', args=[room_id])
        headers = self.feedback_auth_headers(self.creator)
        for _ in range(2):
            response = self.client.post(close_url, {'reason': 'comment_not_helpful'},
                format='json', **headers)
            self.assertEqual(response.status_code, 200)
            self.assertIsNotNone(response.data['closed_at'])
            self.assertFalse(response.data['payout_due'])
            self.assertIsNone(response.data['payout'])
        self.assertFalse(FeedbackPayout.objects.exists())

    def feedback_auth_headers(self, user):
        response = self.client.post(reverse('login'),
            {'username': user.username, 'password': 'password123'}, format='json')
        self.assertEqual(response.status_code, 200)
        return {'HTTP_AUTHORIZATION': 'Bearer ' + response.data['auth_token']}

    def make_unlocked_feedback(self, review):
        submitted = self.client.post(reverse('feedback'),
            {'track_id': self.track.id, 'comment': 'Try adjusting the snare.'},
            format='json', **self.feedback_auth_headers(self.listener))
        self.assertEqual(submitted.status_code, 201)
        feedback = Feedback.objects.get(pk=submitted.data['feedback_id'])
        feedback.grammar_review = review
        feedback.save(update_fields=['grammar_review'])
        unlocked = self.client.post(reverse('unlock_feedback', args=[feedback.id]),
            {'confirm': True}, format='json', **self.feedback_auth_headers(self.creator))
        self.assertEqual(unlocked.status_code, 200)
        feedback.refresh_from_db(fields=['chat_message'])
        return feedback, unlocked.data['chat_room_id']

    def test_ghosting_report_permissions_and_unchanged_deadline(self):
        feedback, room_id = self.make_unlocked_feedback(Feedback.GrammarReview.PASSED)
        url = reverse('report_ghosting', args=[room_id])
        listener = self.feedback_auth_headers(self.listener)
        with patch('django.utils.timezone.now') as clock:
            clock.return_value = datetime(2026, 11, 14, tzinfo=timezone.utc)
            self.assertEqual(self.client.post(url).status_code, 401)
            self.assertEqual(self.client.post(url, **self.feedback_auth_headers(self.creator)).status_code, 403)
            other = User.objects.create_user('other_listener', password='password123')
            self.assertEqual(self.client.post(url, **self.feedback_auth_headers(other)).status_code, 403)
            report = self.client.post(url, **listener)
            self.assertEqual(report.status_code, 201)
            self.assertEqual(report.data['deadline'], clock.return_value + timedelta(days=7))
            clock.return_value += timedelta(days=2)
            retry = self.client.post(url, **listener)
            self.assertEqual(retry.status_code, 200)
            self.assertEqual(retry.data, report.data)
            self.assertEqual(GhostingReport.objects.get().feedback_id, feedback.id)

    def test_ghosting_report_requires_unlocked_open_chat_without_creator_reply(self):
        feedback, room_id = self.make_unlocked_feedback(Feedback.GrammarReview.PASSED)
        room = ChatRoom.objects.get(pk=room_id)
        url = reverse('report_ghosting', args=[room_id])
        listener = self.feedback_auth_headers(self.listener)
        with patch('django.utils.timezone.now', return_value=datetime(2026, 11, 14, tzinfo=timezone.utc)):
            message_id = feedback.chat_message_id
            Feedback.objects.filter(pk=feedback.pk).update(chat_message=None)
            self.assertEqual(self.client.post(url, **listener).status_code, 409)
            Feedback.objects.filter(pk=feedback.pk).update(chat_message_id=message_id)
            reply = self.client.post(reverse('chat_messages', args=[room_id]), {'content': 'Thanks'},
                format='json', **self.feedback_auth_headers(self.creator))
            self.assertEqual(reply.status_code, 201)
            self.assertEqual(self.client.post(url, **listener).status_code, 409)
            room.messages.filter(pk=reply.data['id']).delete()
            closed = self.client.post(reverse('close_chat', args=[room_id]), {'reason': 'comment_not_helpful'},
                format='json', **self.feedback_auth_headers(self.creator))
            self.assertEqual(closed.status_code, 200)
            self.assertEqual(self.client.post(url, **listener).status_code, 409)
        self.assertFalse(GhostingReport.objects.exists())

    def test_early_reply_disqualifies_ghosting_payout(self):
        with patch('django.utils.timezone.now') as clock:
            clock.return_value = datetime(2026, 11, 14, tzinfo=timezone.utc)
            _, room_id = self.make_unlocked_feedback(Feedback.GrammarReview.PASSED)
            deadline = self.report_ghosting_deadline(room_id)
            clock.return_value = deadline - timedelta(seconds=1)
            reply = self.client.post(reverse('chat_messages', args=[room_id]), {'content': 'Thanks'},
                format='json', **self.feedback_auth_headers(self.creator))
            self.assertEqual(reply.status_code, 201)
            clock.return_value = deadline
            call_command('process_ghosting_reports', stdout=StringIO())
        self.assertFalse(FeedbackPayout.objects.exists())
        self.assertEqual(GhostingReport.objects.get().processed_at, deadline)

    def test_early_closure_applies_closure_payout_rule(self):
        for review in (Feedback.GrammarReview.PASSED, Feedback.GrammarReview.FAILED):
            with self.subTest(review=review), patch('django.utils.timezone.now') as clock:
                clock.return_value = datetime(2026, 11, 14, tzinfo=timezone.utc)
                feedback, room_id = self.make_unlocked_feedback(review)
                deadline = self.report_ghosting_deadline(room_id)
                clock.return_value = deadline - timedelta(seconds=1)
                response = self.client.post(reverse('close_chat', args=[room_id]),
                    {'reason': 'comment_not_helpful'}, format='json', **self.feedback_auth_headers(self.creator))
                self.assertEqual(response.status_code, 200)
                expected_count = int(review == Feedback.GrammarReview.PASSED)
                self.assertEqual(FeedbackPayout.objects.filter(feedback=feedback).count(), expected_count)
                clock.return_value = deadline
                output = StringIO()
                call_command('process_ghosting_reports', stdout=output)
                self.assertIn('Recorded 0 new ghosting payouts.', output.getvalue())
                self.assertEqual(FeedbackPayout.objects.filter(feedback=feedback).count(), expected_count)
                ChatRoom.objects.get(pk=room_id).delete()
                feedback.delete()

    def test_expiry_records_one_payout_only_for_passed_review(self):
        for review in (Feedback.GrammarReview.PASSED, Feedback.GrammarReview.FAILED):
            with self.subTest(review=review), patch('django.utils.timezone.now') as clock:
                clock.return_value = datetime(2026, 11, 14, tzinfo=timezone.utc)
                feedback, room_id = self.make_unlocked_feedback(review)
                deadline = self.report_ghosting_deadline(room_id)
                clock.return_value = deadline - timedelta(seconds=1)
                call_command('process_ghosting_reports', stdout=StringIO())
                self.assertIsNone(GhostingReport.objects.get(feedback=feedback).processed_at)
                self.assertFalse(FeedbackPayout.objects.filter(feedback=feedback).exists())

                clock.return_value = deadline
                call_command('process_ghosting_reports', stdout=StringIO())
                expected = [(100, 'EUR')] if review == Feedback.GrammarReview.PASSED else []
                payouts = FeedbackPayout.objects.filter(feedback=feedback)
                self.assertEqual(list(payouts.values_list('amount_cents', 'currency')), expected)
                self.assertEqual(GhostingReport.objects.get(feedback=feedback).processed_at, deadline)
                clock.return_value += timedelta(hours=1)
                call_command('process_ghosting_reports', stdout=StringIO())
                closed = self.client.post(reverse('close_chat', args=[room_id]), {'reason': 'comment_not_helpful'},
                    format='json', **self.feedback_auth_headers(self.creator))
                self.assertEqual(closed.status_code, 200)
                self.assertEqual(list(payouts.values_list('amount_cents', 'currency')), expected)
                ChatRoom.objects.get(pk=room_id).delete()
                feedback.delete()

    def test_reply_after_deadline_does_not_cancel_earned_payout(self):
        with patch('django.utils.timezone.now') as clock:
            clock.return_value = datetime(2026, 11, 14, tzinfo=timezone.utc)
            feedback, room_id = self.make_unlocked_feedback(Feedback.GrammarReview.PASSED)
            clock.return_value = self.report_ghosting_deadline(room_id) + timedelta(seconds=1)
            reply = self.client.post(reverse('chat_messages', args=[room_id]), {'content': 'Thanks'},
                format='json', **self.feedback_auth_headers(self.creator))
            self.assertEqual(reply.status_code, 201)
            call_command('process_ghosting_reports', stdout=StringIO())
            self.assertEqual(FeedbackPayout.objects.get(feedback=feedback).amount_cents, 100)

    def report_ghosting_deadline(self, room_id):
        response = self.client.post(reverse('report_ghosting', args=[room_id]),
            **self.feedback_auth_headers(self.listener))
        self.assertEqual(response.status_code, 201)
        return response.data['deadline']

    def fixture_auth_headers(self, user):
        token, _ = Token.objects.get_or_create(user=user)
        return {'HTTP_AUTHORIZATION': 'Bearer ' + token.key}

    def test_client_supplied_identity_cannot_override_authentication(self):
        feedback, room_id = self.make_unlocked_feedback(Feedback.GrammarReview.PASSED)
        owner_id = str(self.creator.pk)
        messages = reverse('chat_messages', args=[room_id])
        close = reverse('close_chat', args=[room_id])
        report = reverse('report_ghosting', args=[room_id])
        unlock = reverse('unlock_feedback', args=[feedback.pk])
        summary = reverse('track_feedback', args=[self.track.pk])
        consent = reverse('blockchain_consent', args=[feedback.chat_message_id])
        for method, url in (
            ('get', reverse('tracks')), ('get', reverse('deck')), ('get', reverse('saved_tracks')),
            ('post', reverse('feedback')), ('get', reverse('chats')),
            ('get', messages), ('post', messages), ('post', close), ('post', report),
            ('post', unlock), ('get', summary), ('post', summary), ('post', consent),
        ):
            with self.subTest(method=method, url=url):
                request = getattr(self.client, method)
                self.assertEqual(request(url, HTTP_X_USER_ID=owner_id).status_code, 401)

        self.assertEqual(self.client.get(messages, HTTP_AUTHORIZATION='Bearer invalid',
            HTTP_X_USER_ID=owner_id).status_code, 401)
        outsider = User.objects.create_user('identity_outsider')
        headers = {**self.fixture_auth_headers(outsider), 'HTTP_X_USER_ID': owner_id}
        self.assertEqual(self.client.get(messages, **headers).status_code, 403)
        self.assertEqual(self.client.post(messages, {'content': 'Outsider'},
            format='json', **headers).status_code, 403)
        self.assertEqual(self.client.post(consent, {'action': 'request'},
            format='json', **headers).status_code, 403)

        listener = {**self.feedback_auth_headers(self.listener), 'HTTP_X_USER_ID': owner_id}
        for url, payload in (
            (unlock, {'confirm': True}), (summary, {}), (close, {'reason': 'comment_not_helpful'}),
        ):
            self.assertEqual(self.client.post(url, {**payload, 'user_id': owner_id},
                format='json', **listener).status_code, 403)
        self.assertEqual(self.client.post(report, **listener).status_code, 201)
        reply = self.client.post(messages, {'content': 'Listener follow-up', 'sender_id': owner_id},
            format='json', **listener)
        self.assertEqual(reply.status_code, 201)
        self.assertEqual(reply.data['sender_id'], self.listener.pk)

        owner = {**self.feedback_auth_headers(self.creator), 'HTTP_X_USER_ID': str(self.listener.pk)}
        self.assertEqual(self.client.post(report, **owner).status_code, 403)
        self.assertEqual(self.client.post(consent, {'action': 'request'},
            format='json', **owner).status_code, 403)
        self.assertEqual(self.client.post(consent, {'action': 'request'},
            format='json', **listener).status_code, 200)
        self.assertEqual(self.client.post(consent, {'action': 'approve'},
            format='json', **listener).status_code, 403)
        self.assertEqual(self.client.post(consent, {'action': 'approve'},
            format='json', **owner).status_code, 200)
        self.assertEqual(self.client.post(close, {'reason': 'comment_not_helpful'},
            format='json', **owner).status_code, 200)
