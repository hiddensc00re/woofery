from django.contrib.auth.models import User
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from .models import UserProfile, Track, ChatMessage

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
        
        # Call with X-User-ID header
        response = self.client.get(url, HTTP_X_USER_ID=str(self.listener.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['track_name'], 'Test Track')

    def test_feedback_unlock_lifecycle(self):
        def authenticate_for_feedback(user):
            login = self.client.post(reverse('login'), {
                'username': user.username, 'password': 'password123',
            }, format='json')
            self.assertEqual(login.status_code, 200)
            return {'HTTP_AUTHORIZATION': 'Bearer ' + login.data['feedback_token']}

        creator_headers = authenticate_for_feedback(self.creator)
        listener_headers = authenticate_for_feedback(self.listener)
        chat_headers = {'HTTP_X_USER_ID': str(self.creator.id)}
        summary_url = reverse('track_feedback', args=[self.track.id])
        listeners = [self.listener] + [
            User.objects.create_user(username=f'listener_{i}') for i in range(4)
        ]
        feedback_ids = []
        for number, listener in enumerate(listeners, 1):
            submission = self.client.post(reverse('feedback'), {
                'track_id': self.track.id, 'comment': f'Private comment {number}',
            }, format='json', HTTP_X_USER_ID=str(listener.id))
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
                format='json', **chat_headers).status_code, 401)
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
