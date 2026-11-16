import os
import hashlib
from django.shortcuts import render, get_object_or_404
from rest_framework.decorators import api_view, parser_classes
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser, FormParser
from django.contrib.auth import authenticate
from django.core import signing
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from .models import (
    ChatMessage, ChatRoom, Feedback, FeedbackPayout, SavedTrack, Track, UserProfile,
)

def get_user_from_request(request):
    """
    Helper to get user from session, token, or X-User-ID header for easy frontend integration.
    """
    if request.user.is_authenticated:
        return request.user
    
    user_id = request.headers.get('X-User-ID')
    if user_id:
        try:
            return User.objects.get(id=int(user_id))
        except (ValueError, User.DoesNotExist):
            pass
    return None

@api_view(['POST'])
def register_user(request):
    username = request.data.get('username')
    password = request.data.get('password')
    role = request.data.get('role', 'listener')
    
    if not username or not password:
        return Response({'error': 'Username and password required'}, status=status.HTTP_400_BAD_REQUEST)
        
    if User.objects.filter(username=username).exists():
        return Response({'error': 'Username already exists'}, status=status.HTTP_400_BAD_REQUEST)
        
    user = User.objects.create_user(username=username, password=password)
    profile = UserProfile.objects.create(user=user, role=role)
    
    return Response({
        'user_id': user.id,
        'username': user.username,
        'role': profile.role,
        'feedback_token': signing.dumps(user.id, salt='feedback-api'),
    }, status=status.HTTP_201_CREATED)

@api_view(['POST'])
def login_user(request):
    username = request.data.get('username')
    password = request.data.get('password')
    
    if not username or not password:
        return Response({'error': 'Username and password required'}, status=status.HTTP_400_BAD_REQUEST)
        
    user = authenticate(username=username, password=password)
    if user is not None:
        profile, created = UserProfile.objects.get_or_create(user=user)
        return Response({
            'user_id': user.id,
            'username': user.username,
            'role': profile.role,
            'feedback_token': signing.dumps(user.id, salt='feedback-api'),
        })
    else:
        return Response({'error': 'Invalid credentials'}, status=status.HTTP_401_UNAUTHORIZED)

@api_view(['GET', 'POST'])
@parser_classes([MultiPartParser, FormParser])
def track_list_create(request):
    user = get_user_from_request(request)
    if not user:
        return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)

    if request.method == 'POST':
        profile = getattr(user, 'profile', None)
        if not profile or profile.role != 'creator':
            return Response({'error': 'Only creators can upload tracks'}, status=status.HTTP_403_FORBIDDEN)
            
        artist_name = request.data.get('artist_name', user.username)
        track_name = request.data.get('track_name')
        audio_file = request.FILES.get('audio_file')
        cover_image = request.FILES.get('cover_image')
        target_feedback = request.data.get('target_feedback', '')
        max_listeners = int(request.data.get('max_listeners', 5))
        
        if not track_name or not audio_file:
            return Response({'error': 'Track name and audio file are required'}, status=status.HTTP_400_BAD_REQUEST)
            
        track = Track.objects.create(
            uploader=user,
            artist_name=artist_name,
            track_name=track_name,
            audio_file=audio_file,
            cover_image=cover_image,
            target_feedback=target_feedback,
            max_listeners=min(max_listeners, 5),
            is_active=True
        )
        
        return Response({
            'id': track.id,
            'artist_name': track.artist_name,
            'track_name': track.track_name,
            'audio_url': track.audio_file.url,
            'cover_url': track.cover_image.url if track.cover_image else None,
            'target_feedback': track.target_feedback,
            'max_listeners': track.max_listeners
        }, status=status.HTTP_201_CREATED)
        
    elif request.method == 'GET':
        profile = getattr(user, 'profile', None)
        if profile and profile.role == 'creator':
            tracks = Track.objects.filter(uploader=user)
        else:
            tracks = Track.objects.filter(is_active=True)
            
        data = []
        for t in tracks:
            feedback_count = t.feedbacks.count()
            data.append({
                'id': t.id,
                'artist_name': t.artist_name,
                'track_name': t.track_name,
                'audio_url': t.audio_file.url,
                'cover_url': t.cover_image.url if t.cover_image else None,
                'target_feedback': t.target_feedback,
                'max_listeners': t.max_listeners,
                'feedback_count': feedback_count,
                'is_active': t.is_active,
                'created_at': t.created_at
            })
        return Response(data)

@api_view(['GET'])
def get_swipe_deck(request):
    user = get_user_from_request(request)
    if not user:
        return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
        
    commented_track_ids = Feedback.objects.filter(listener=user).values_list('track_id', flat=True)
    deck_tracks = Track.objects.filter(is_active=True).exclude(id__in=commented_track_ids).exclude(uploader=user)
    deck_tracks = deck_tracks.order_by('?')
    
    data = []
    for t in deck_tracks:
        data.append({
            'id': t.id,
            'artist_name': t.artist_name,
            'track_name': t.track_name,
            'audio_url': t.audio_file.url,
            'cover_url': t.cover_image.url if t.cover_image else None,
            'target_feedback': t.target_feedback,
            'max_listeners': t.max_listeners,
            'feedback_count': t.feedbacks.count()
        })
    return Response(data)

@api_view(['POST'])
@transaction.atomic
def submit_feedback(request):
    user = get_user_from_request(request)
    if not user:
        return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
        
    track_id = request.data.get('track_id')
    comment = request.data.get('comment')
    
    if not track_id or not comment:
        return Response({'error': 'Track ID and comment are required'}, status=status.HTTP_400_BAD_REQUEST)
        
    try:
        track = Track.objects.select_for_update().get(id=int(track_id))
    except (ValueError, Track.DoesNotExist):
        return Response({'error': 'Track not found'}, status=status.HTTP_404_NOT_FOUND)
        
    if track.uploader_id == user.id:
        return Response({'error': 'The creator cannot review their own track'}, status=403)

    if not track.is_active:
        return Response({'error': 'Track is no longer accepting feedback'}, status=status.HTTP_400_BAD_REQUEST)
        
    if Feedback.objects.filter(track=track, listener=user).exists():
        return Response({'error': 'You already left feedback on this track'}, status=status.HTTP_400_BAD_REQUEST)
        
    feedback = Feedback.objects.create(
        track=track,
        listener=user,
        comment=comment
    )
    
    SavedTrack.objects.filter(user=user, track=track).delete()
    
    current_count = track.feedbacks.count()
    if current_count >= track.max_listeners:
        track.is_active = False
        track.save()
        
    return Response({
        'status': 'feedback_saved',
        'feedback_id': feedback.id,
        'track_active': track.is_active
    }, status=status.HTTP_201_CREATED)

def get_feedback_api_user(request):
    if request.user.is_authenticated:
        return request.user
    scheme, _, token = request.headers.get('Authorization', '').partition(' ')
    if scheme.lower() != 'bearer' or not token:
        return None
    try:
        user_id = signing.loads(token, salt='feedback-api', max_age=3600)
    except signing.BadSignature:
        return None
    return User.objects.filter(pk=user_id, is_active=True).first()


@api_view(['GET', 'POST'])
@transaction.atomic
def track_feedback(request, track_id):
    user = get_feedback_api_user(request)
    if not user:
        return Response({'error': 'Authentication required'}, status=401)
    tracks = Track.objects.select_for_update() if request.method == 'POST' else Track.objects.all()
    track = get_object_or_404(tracks, pk=track_id)
    if track.uploader_id != user.id:
        return Response({'error': 'Only the track creator can manage feedback'}, status=403)

    comments = list(track.feedbacks.order_by('id').values('id', 'chat_message_id'))
    any_unlocked = any(comment['chat_message_id'] is not None for comment in comments)
    can_decline = len(comments) >= track.max_listeners and not any_unlocked and not track.feedback_declined
    if request.method == 'POST' and not track.feedback_declined:
        if not can_decline:
            return Response({'error': 'Declining requires the full set of locked comments'}, status=409)
        track.feedback_declined = True
        track.save(update_fields=['feedback_declined'])

    return Response({
        'feedback_count': len(comments),
        'max_listeners': track.max_listeners,
        'declined': track.feedback_declined,
        'can_decline': can_decline and not track.feedback_declined,
        'can_unlock': not track.feedback_declined and any(
            comment['chat_message_id'] is None for comment in comments
        ),
        'comments': [
            {'id': comment['id'], 'unlocked': comment['chat_message_id'] is not None}
            for comment in comments
        ],
    })


@api_view(['POST'])
@transaction.atomic
def unlock_feedback(request, feedback_id):
    user = get_feedback_api_user(request)
    if not user:
        return Response({'error': 'Authentication required'}, status=401)
    feedback = get_object_or_404(Feedback, pk=feedback_id)
    track = get_object_or_404(Track.objects.select_for_update(), pk=feedback.track_id)
    feedback.refresh_from_db(fields=['chat_message'])
    if track.uploader_id != user.id:
        return Response({'error': 'Only the track creator can unlock feedback'}, status=403)
    if track.feedback_declined:
        return Response({'error': 'Feedback was declined for this track'}, status=409)
    if request.data.get('confirm') is not True:
        return Response({'error': 'Payment confirmation is required'}, status=400)

    already_unlocked = feedback.chat_message_id is not None
    if not already_unlocked:
        room, _ = ChatRoom.objects.select_for_update().get_or_create(
            track=track, listener=feedback.listener, uploader=track.uploader
        )
        if room.closed_at is not None:
            return Response({'error': 'This chat is closed'}, status=409)
        feedback.chat_message = ChatMessage.objects.create(
            room=room, sender=feedback.listener, content=feedback.comment
        )
        feedback.save(update_fields=['chat_message'])

    return Response({
        'feedback_id': feedback.id,
        'chat_room_id': feedback.chat_message.room_id,
        'message_id': feedback.chat_message_id,
        'already_unlocked': already_unlocked,
        'payment': {'simulated': True, 'amount_cents': 499, 'currency': 'EUR'},
    })


@api_view(['GET', 'POST'])
def chat_rooms_list(request):
    user = get_user_from_request(request)
    if not user:
        return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
        
    if request.method == 'GET':
        rooms = ChatRoom.objects.filter(Q(listener=user) | Q(uploader=user)).order_by('-created_at')
        data = []
        for r in rooms:
            last_msg = r.messages.order_by('-timestamp').first()
            other_user = r.uploader if r.listener == user else r.listener
            data.append({
                'id': r.id,
                'track_id': r.track.id,
                'closed_at': r.closed_at,
                'close_reason': r.close_reason,
                'track_name': r.track.track_name,
                'artist_name': r.track.artist_name,
                'other_username': other_user.username,
                'other_role': other_user.profile.role if hasattr(other_user, 'profile') else 'listener',
                'last_message': last_msg.content if last_msg else '',
                'last_message_time': last_msg.timestamp if last_msg else r.created_at
            })
        return Response(data)

@api_view(['POST'])
@transaction.atomic
def close_chat(request, room_id):
    user = get_feedback_api_user(request)
    if not user:
        return Response({'error': 'Authentication required'}, status=401)

    room = get_object_or_404(ChatRoom.objects.select_for_update(), pk=room_id)
    if room.track.uploader_id != user.id:
        return Response({'error': 'Only the track creator can close this chat'}, status=403)
    if request.data.get('reason') != ChatRoom.CloseReason.COMMENT_NOT_HELPFUL:
        return Response({'error': 'Invalid close reason'}, status=400)

    feedback = Feedback.objects.filter(
        chat_message__room=room, track_id=room.track_id, listener_id=room.listener_id,
    ).first()
    if room.track.feedback_declined or feedback is None:
        return Response({'error': 'This chat has no eligible unlocked feedback'}, status=409)

    if room.closed_at is None:
        room.close_reason = ChatRoom.CloseReason.COMMENT_NOT_HELPFUL
        room.closed_at = timezone.now()
        room.save(update_fields=['close_reason', 'closed_at'])
        if feedback.grammar_review == Feedback.GrammarReview.PASSED:
            FeedbackPayout.objects.get_or_create(
                feedback=feedback, defaults={'amount_cents': 100, 'currency': 'EUR'},
            )

    payout = FeedbackPayout.objects.filter(feedback=feedback).first()
    return Response({
        'chat_room_id': room.id,
        'closed_at': room.closed_at,
        'close_reason': room.close_reason,
        'payout_due': payout is not None,
        'payout': {'amount_cents': payout.amount_cents, 'currency': payout.currency} if payout else None,
    })


@api_view(['GET', 'POST'])
@transaction.atomic
def chat_messages(request, room_id):
    user = get_user_from_request(request)
    if not user:
        return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
        
    try:
        rooms = ChatRoom.objects.select_for_update() if request.method == 'POST' else ChatRoom.objects.all()
        room = rooms.get(pk=room_id)
    except (ValueError, ChatRoom.DoesNotExist):
        return Response({'error': 'Chat room not found'}, status=status.HTTP_404_NOT_FOUND)
        
    if room.listener != user and room.uploader != user:
        return Response({'error': 'Access denied'}, status=status.HTTP_403_FORBIDDEN)
        
    if request.method == 'POST':
        if room.closed_at is not None:
            return Response({'error': 'This chat is closed'}, status=409)
        content = request.data.get('content')
        if not content:
            return Response({'error': 'Message content required'}, status=status.HTTP_400_BAD_REQUEST)
            
        msg = ChatMessage.objects.create(
            room=room,
            sender=user,
            content=content
        )
        return Response({
            'id': msg.id,
            'sender_id': msg.sender.id,
            'sender_username': msg.sender.username,
            'content': msg.content,
            'timestamp': msg.timestamp,
            'is_blockchain_pending': msg.is_blockchain_pending,
            'is_blockchain_written': msg.is_blockchain_written,
            'blockchain_tx_hash': msg.blockchain_tx_hash
        }, status=status.HTTP_201_CREATED)
        
    elif request.method == 'GET':
        messages = room.messages.order_by('timestamp')
        data = []
        for m in messages:
            data.append({
                'id': m.id,
                'sender_id': m.sender.id,
                'sender_username': m.sender.username,
                'content': m.content,
                'timestamp': m.timestamp,
                'is_blockchain_pending': m.is_blockchain_pending,
                'is_blockchain_written': m.is_blockchain_written,
                'blockchain_tx_hash': m.blockchain_tx_hash
            })
        return Response(data)

@api_view(['POST'])
def blockchain_consent(request, message_id):
    user = get_user_from_request(request)
    if not user:
        return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
        
    try:
        msg = ChatMessage.objects.get(id=int(message_id))
    except (ValueError, ChatMessage.DoesNotExist):
        return Response({'error': 'Message not found'}, status=status.HTTP_404_NOT_FOUND)
        
    room = msg.room
    if room.listener != user and room.uploader != user:
        return Response({'error': 'Access denied'}, status=status.HTTP_403_FORBIDDEN)
        
    action = request.data.get('action')
    
    if action == 'request':
        msg.is_blockchain_pending = True
        msg.save()
        return Response({
            'id': msg.id,
            'is_blockchain_pending': msg.is_blockchain_pending,
            'is_blockchain_written': msg.is_blockchain_written
        })
        
    elif action == 'approve':
        if not msg.is_blockchain_pending:
            return Response({'error': 'No blockchain request pending for this message'}, status=status.HTTP_400_BAD_REQUEST)
            
        payload = f"{msg.id}-{msg.content}-{room.listener.username}-{room.uploader.username}".encode('utf-8')
        mock_hash = hashlib.sha256(payload).hexdigest()
        
        msg.is_blockchain_pending = False
        msg.is_blockchain_written = True
        msg.blockchain_tx_hash = f"0x{mock_hash}"
        msg.save()
        
        return Response({
            'id': msg.id,
            'is_blockchain_pending': msg.is_blockchain_pending,
            'is_blockchain_written': msg.is_blockchain_written,
            'blockchain_tx_hash': msg.blockchain_tx_hash
        })
        
    else:
        return Response({'error': 'Invalid action'}, status=status.HTTP_400_BAD_REQUEST)

@api_view(['GET', 'POST', 'DELETE'])
def saved_tracks_view(request):
    user = get_user_from_request(request)
    if not user:
        return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
        
    if request.method == 'GET':
        saved = SavedTrack.objects.filter(user=user)
        data = []
        for s in saved:
            t = s.track
            data.append({
                'id': t.id,
                'artist_name': t.artist_name,
                'track_name': t.track_name,
                'audio_url': t.audio_file.url,
                'cover_url': t.cover_image.url if t.cover_image else None,
                'target_feedback': t.target_feedback,
                'saved_at': s.saved_at
            })
        return Response(data)
        
    elif request.method == 'POST':
        track_id = request.data.get('track_id')
        if not track_id:
            return Response({'error': 'Track ID required'}, status=status.HTTP_400_BAD_REQUEST)
            
        try:
            track = Track.objects.get(id=int(track_id))
        except (ValueError, Track.DoesNotExist):
            return Response({'error': 'Track not found'}, status=status.HTTP_404_NOT_FOUND)
            
        saved_track, created = SavedTrack.objects.get_or_create(user=user, track=track)
        return Response({'status': 'track_saved', 'created': created}, status=status.HTTP_201_CREATED)
        
    elif request.method == 'DELETE':
        track_id = request.query_params.get('track_id') or request.data.get('track_id')
        if not track_id:
            return Response({'error': 'Track ID required'}, status=status.HTTP_400_BAD_REQUEST)
            
        SavedTrack.objects.filter(user=user, track_id=int(track_id)).delete()
        return Response({'status': 'track_unsaved'})

@api_view(['GET'])
def api_root(request):
    return Response({
        'name': 'SoundSwipe API',
        'status': 'online',
        'version': '1.0.0',
        'endpoints': {
            'register': '/api/register/',
            'login': '/api/login/',
            'tracks': '/api/tracks/',
            'deck': '/api/deck/',
            'feedback': '/api/feedback/',
            'chats': '/api/chats/',
            'saved': '/api/saved/'
        }
    })
