from flask import Flask, request, jsonify, render_template_string, send_file
from flask_sqlalchemy import SQLAlchemy
from twilio.twiml.voice_response import VoiceResponse, Start, Dial
import os
import json
import requests
from datetime import datetime, timedelta
from functools import wraps

app = Flask(__name__)

DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://koyeb-adm:npg_19AiOJgmEZYB@ep-jolly-night-a23g0dl0.eu-central-1.pg.koyeb.app/koyebdb')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_size': 10,
    'pool_recycle': 280,
    'pool_pre_ping': True,
    'pool_timeout': 30,
    'max_overflow': 20
}

db = SQLAlchemy(app)

ELEVENLABS_PHONE_NUMBER = os.environ.get('ELEVENLABS_PHONE_NUMBER', '+447366432353')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID', 'your_twilio_sid')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', 'your_twilio_token')

# Database retry decorator
def with_db_retry(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if 'SSL connection' in str(e) or 'server closed' in str(e):
                    print(f"DB connection lost (attempt {attempt + 1}/{max_retries})")
                    db.session.rollback()
                    db.session.remove()
                    db.engine.dispose()
                    if attempt < max_retries - 1:
                        continue
                raise e
        return func(*args, **kwargs)
    return wrapper

# Database Models
class Call(db.Model):
    __tablename__ = 'calls'
    
    id = db.Column(db.Integer, primary_key=True)
    call_sid = db.Column(db.String(100), unique=True, nullable=False, index=True)
    from_number = db.Column(db.String(50))
    start_time = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(20), default='active')
    
    customer_name = db.Column(db.String(100))
    postcode = db.Column(db.String(20))
    service = db.Column(db.String(200))
    customer_address = db.Column(db.Text)
    customer_email = db.Column(db.String(100))
    callback_requested = db.Column(db.Boolean, default=False)
    trade_customer = db.Column(db.Boolean, default=False)
    when_needed = db.Column(db.String(100))
    
    # Recording fields
    recording_url = db.Column(db.String(500))
    recording_sid = db.Column(db.String(100))
    recording_duration = db.Column(db.Integer, default=0)
    local_audio_path = db.Column(db.String(300))
    audio_status = db.Column(db.String(50), default='pending')
    
    # Team notes
    team_notes = db.Column(db.Text)
    
    skip_size = db.Column(db.String(50))
    waste_type = db.Column(db.String(200))
    placement_location = db.Column(db.String(100))
    delivery_date = db.Column(db.String(100))
    time_preference = db.Column(db.String(20))
    skip_price = db.Column(db.String(50))
    booking_confirmed = db.Column(db.Boolean, default=False)
    
    yards_requested = db.Column(db.String(50))
    supplements = db.Column(db.String(200))
    stairs_access = db.Column(db.String(50))
    
    grab_size = db.Column(db.String(50))
    material_type = db.Column(db.String(100))
    roadside_reach = db.Column(db.Boolean, default=True)
    
    transcripts = db.relationship('Transcript', backref='call', lazy=True, cascade='all, delete-orphan')
    
    def to_dict(self):
        transcripts_data = [t.to_dict() for t in self.transcripts]
        
        return {
            'id': self.id,
            'call_sid': self.call_sid,
            'from': self.from_number,
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'timestamp': (self.start_time + timedelta(hours=1)).strftime('%d/%m/%Y %H:%M:%S') if self.start_time else None,
            'status': self.status,
            'transcript_count': len(self.transcripts),
            'customer_name': self.customer_name,
            'phone': self.from_number,
            'email': self.customer_email,
            'address': self.customer_address,
            'postcode': self.postcode,
            'service': self.service,
            'trade_customer': self.trade_customer,
            'callback_requested': self.callback_requested,
            'when_needed': self.when_needed,
            'recording_url': self.recording_url,
            'recording_duration': self.recording_duration,
            'local_audio_path': self.local_audio_path,
            'audio_status': self.audio_status,
            'team_notes': self.team_notes,
            'skip_size': self.skip_size,
            'waste_type': self.waste_type,
            'placement_location': self.placement_location,
            'delivery_date': self.delivery_date,
            'time_preference': self.time_preference,
            'skip_price': self.skip_price,
            'booking_confirmed': self.booking_confirmed,
            'yards_requested': self.yards_requested,
            'supplements': self.supplements,
            'stairs_access': self.stairs_access,
            'grab_size': self.grab_size,
            'material_type': self.material_type,
            'roadside_reach': self.roadside_reach,
            'transcript': transcripts_data,
        }

class Transcript(db.Model):
    __tablename__ = 'transcripts'
    
    id = db.Column(db.Integer, primary_key=True)
    call_sid = db.Column(db.String(100), db.ForeignKey('calls.call_sid'), nullable=False, index=True)
    speaker = db.Column(db.String(20))
    text = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    
    def to_dict(self):
        return {
            'speaker': self.speaker,
            'text': self.text,
            'message': self.text,
            'role': self.speaker.lower() if self.speaker else 'unknown',
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'time': self.timestamp.strftime('%H:%M:%S') if self.timestamp else None
        }

class LiveCall(db.Model):
    __tablename__ = 'live_calls'
    
    id = db.Column(db.Integer, primary_key=True)
    call_sid = db.Column(db.String(150), unique=True)
    customer_name = db.Column(db.String(100), default='Unknown')
    phone = db.Column(db.String(20), default='Unknown')
    postcode = db.Column(db.String(10), default='Unknown')
    status = db.Column(db.String(50), default='connecting')
    start_time = db.Column(db.DateTime, default=datetime.utcnow)
    last_update = db.Column(db.DateTime, default=datetime.utcnow)

with app.app_context():
    db.create_all()

# Cleanup Functions
def cleanup_old_audio_files():
    try:
        audio_dir = os.path.join(os.getcwd(), 'audio_files')
        if not os.path.exists(audio_dir):
            return
        
        two_hours_ago = datetime.utcnow() - timedelta(hours=2)
        deleted_count = 0
        
        for filename in os.listdir(audio_dir):
            filepath = os.path.join(audio_dir, filename)
            if os.path.isfile(filepath):
                file_time = datetime.fromtimestamp(os.path.getmtime(filepath))
                if file_time < two_hours_ago:
                    os.remove(filepath)
                    deleted_count += 1
        
        if deleted_count > 0:
            print(f"Cleanup: Deleted {deleted_count} audio files")
    except Exception as e:
        print(f"Audio cleanup error: {e}")

def cleanup_old_database_records():
    try:
        three_months_ago = datetime.utcnow() - timedelta(days=90)
        
        old_transcripts = Transcript.query.filter(Transcript.timestamp < three_months_ago).all()
        for transcript in old_transcripts:
            db.session.delete(transcript)
        
        old_calls = Call.query.filter(Call.start_time < three_months_ago).all()
        for call in old_calls:
            db.session.delete(call)
        
        old_live_calls = LiveCall.query.filter(LiveCall.start_time < three_months_ago).all()
        for live_call in old_live_calls:
            db.session.delete(live_call)
        
        db.session.commit()
        
        total_deleted = len(old_transcripts) + len(old_calls) + len(old_live_calls)
        if total_deleted > 0:
            print(f"Cleanup: Deleted {total_deleted} old records")
    except Exception as e:
        db.session.rollback()
        print(f"Database cleanup error: {e}")

# OpenAI Extraction
def extract_information_with_openai(text, call):
    if not OPENAI_API_KEY:
        return False
        
    recent_transcripts = db.session.query(Transcript.speaker, Transcript.text).filter(
        Transcript.call_sid == call.call_sid
    ).order_by(Transcript.timestamp.desc()).limit(5).all()

    context_messages = [
        {"role": "user" if t.speaker == 'CUSTOMER' else "assistant", "content": t.text}
        for t in reversed(recent_transcripts)
    ]
    context_messages.append({"role": "user", "content": text})

    prompt = """Extract key details from this waste management call.

Conversation:
{conversation_context}

Rules:
- If mentions "skip" → Service is "Skip Hire"
- If mentions "man and van" or "clearance" without "skip" → Service is "Man & Van"  
- If mentions "grab" → Service is "Grab Hire"

Return JSON:
{{"customer_name": "", "postcode": "", "service": "", "trade_customer": null, "skip_size": null, "waste_type": "", "callback_requested": null, "when_needed": ""}}
"""
    
    conversation_context_str = "\n".join([f"{msg['role'].upper()}: {msg['content']}" for msg in context_messages])

    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt.format(conversation_context=conversation_context_str)}],
                "max_tokens": 250,
                "temperature": 0.1,
                "response_format": {"type": "json_object"}
            },
            timeout=10
        )
        
        if response.status_code == 200:
            result = response.json()
            extracted_text = result['choices'][0]['message']['content'].strip()
            extracted_data = json.loads(extracted_text)
            updated = False
            
            for field, value in extracted_data.items():
                if hasattr(call, field) and value is not None and value != "":
                    current_value = getattr(call, field)
                    if current_value is None or current_value == "":
                        setattr(call, field, value)
                        updated = True
            
            return updated
        return False
            
    except Exception as e:
        print(f"OpenAI extraction error: {e}")
        return False

def download_twilio_audio(audio_url, recording_sid):
    try:
        audio_dir = os.path.join(os.getcwd(), 'audio_files')
        os.makedirs(audio_dir, exist_ok=True)
        
        headers = {}
        if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_ACCOUNT_SID != 'your_twilio_sid':
            import base64
            credentials = base64.b64encode(f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode()).decode()
            headers['Authorization'] = f'Basic {credentials}'
        
        response = requests.get(audio_url, headers=headers, stream=True)
        
        if response.status_code == 200:
            filename = f"{recording_sid}.mp3"
            local_path = os.path.join(audio_dir, filename)
            
            with open(local_path, 'wb') as audio_file:
                for chunk in response.iter_content(chunk_size=8192):
                    audio_file.write(chunk)
            
            file_size = os.path.getsize(local_path)
            
            return {
                'success': True,
                'local_path': local_path,
                'file_size': file_size
            }
        else:
            return {
                'success': False,
                'message': f'Failed: {response.status_code}'
            }
            
    except Exception as e:
        print(f"Audio download error: {e}")
        return {'success': False, 'message': str(e)}

# TWILIO ROUTES
@app.route('/voice/incoming', methods=['POST', 'GET'])
@with_db_retry
def handle_incoming_call():
    call_sid = request.form.get('CallSid')
    from_number = request.form.get('From')
    
    try:
        call = Call.query.filter_by(call_sid=call_sid).first()
        if not call:
            call = Call(call_sid=call_sid, from_number=from_number, status='active')
            db.session.add(call)
            db.session.commit()
            
        live_call = LiveCall.query.filter_by(call_sid=call_sid).first()
        if not live_call:
            live_call = LiveCall(call_sid=call_sid, phone=from_number, status='connecting')
            db.session.add(live_call)
            db.session.commit()
    except:
        db.session.rollback()

    response = VoiceResponse()
    start = Start()
    start.transcription(
        statusCallbackUrl=f'https://{request.host}/voice/transcription',
        track='both_tracks',
        partialResults=True,
        languageCode='en-US'
    )
    response.append(start)
    response.pause(length=1)
    
    dial = Dial(
        timeout=30,
        record='record-from-answer',
        recordingStatusCallback=f'https://{request.host}/voice/recording',
        recordingStatusCallbackEvent='completed'
    )
    dial.number(ELEVENLABS_PHONE_NUMBER)
    response.append(dial)
    
    return str(response)

@app.route('/voice/recording', methods=['POST'])
@with_db_retry
def handle_recording():
    call_sid = request.form.get('CallSid')
    recording_sid = request.form.get('RecordingSid')
    recording_duration = request.form.get('RecordingDuration', 0)
    
    try:
        call = Call.query.filter_by(call_sid=call_sid).first()
        if call and recording_sid and TWILIO_ACCOUNT_SID != 'your_twilio_sid':
            # PUBLIC URL - NO PASSWORD
            call.recording_url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Recordings/{recording_sid}.mp3"
            call.recording_sid = recording_sid
            call.recording_duration = int(recording_duration)
            call.audio_status = 'available'
            db.session.commit()
    except:
        db.session.rollback()
    
    return "OK", 200

@app.route('/voice/transcription', methods=['POST', 'GET'])
@with_db_retry
def handle_transcription():
    call_sid = request.form.get('CallSid')
    event = request.form.get('TranscriptionEvent')
    
    if event == 'transcription-content':
        transcription_data = request.form.get('TranscriptionData', '{}')
        try:
            data = json.loads(transcription_data)
            text = data.get('transcript', '').strip()
        except:
            text = ''
        
        track = request.form.get('Track', 'unknown')
        final = request.form.get('Final', 'false').lower() == 'true'
        
        if text and final:
            speaker = 'CUSTOMER' if track == 'inbound_track' else 'AI_AGENT'
            
            try:
                call = Call.query.filter_by(call_sid=call_sid).first()
                if call:
                    transcript = Transcript(call_sid=call_sid, speaker=speaker, text=text)
                    db.session.add(transcript)
                    
                    if speaker == 'CUSTOMER':
                        extract_information_with_openai(text, call)
                    
                    live_call = LiveCall.query.filter_by(call_sid=call_sid).first()
                    if live_call:
                        live_call.status = 'in_progress'
                        live_call.last_update = datetime.utcnow()
                        if call.customer_name:
                            live_call.customer_name = call.customer_name
                        if call.postcode:
                            live_call.postcode = call.postcode
                    
                    db.session.commit()
            except:
                db.session.rollback()
    
    elif event == 'transcription-stopped':
        try:
            call = Call.query.filter_by(call_sid=call_sid).first()
            if call:
                call.status = 'ended'
            
            live_call = LiveCall.query.filter_by(call_sid=call_sid).first()
            if live_call:
                live_call.status = 'completed'
            
            db.session.commit()
        except:
            db.session.rollback()
            
    return "OK", 200

@app.route('/api/conversations')
@with_db_retry
def get_conversations():
    try:
        import random
        if random.randint(1, 10) == 1:
            cleanup_old_audio_files()
        
        if random.randint(1, 100) == 1:
            cleanup_old_database_records()
        
        calls = Call.query.order_by(Call.start_time.desc()).limit(100).all()
        return jsonify({'calls': [call.to_dict() for call in calls]})
    except Exception as e:
        print(f"Error in get_conversations: {e}")
        return jsonify({'calls': []}), 500

@app.route('/api/conversations/<call_sid>')
@with_db_retry
def get_conversation(call_sid):
    try:
        call = Call.query.filter_by(call_sid=call_sid).first()
        
        if not call:
            return jsonify({
                'call_sid': call_sid,
                'call_info': {'from': 'Unknown', 'start_time': datetime.now().isoformat(), 'status': 'unknown'},
                'transcripts': []
            })
        
        transcripts = Transcript.query.filter_by(call_sid=call_sid).order_by(Transcript.timestamp).all()
        
        return jsonify({
            'call_sid': call_sid,
            'call_info': call.to_dict(),
            'transcripts': [t.to_dict() for t in transcripts]
        })
    except:
        return jsonify({
            'call_sid': call_sid,
            'call_info': None,
            'transcripts': []
        }), 500

@app.route('/api/live-calls')
@with_db_retry
def get_live_calls():
    try:
        old_calls = LiveCall.query.filter(
            LiveCall.last_update < datetime.utcnow() - timedelta(hours=1)
        ).all()
        for old_call in old_calls:
            db.session.delete(old_call)
        db.session.commit()
        
        live_calls = LiveCall.query.filter(
            LiveCall.status != 'completed'
        ).order_by(LiveCall.start_time.desc()).all()
        
        calls_data = []
        for call in live_calls:
            calls_data.append({
                'call_sid': call.call_sid,
                'customer_name': call.customer_name,
                'phone': call.phone,
                'postcode': call.postcode,
                'status': call.status,
                'start_time': call.start_time.isoformat(),
                'duration': int((datetime.utcnow() - call.start_time).total_seconds())
            })
        
        return jsonify({'success': True, 'live_calls': calls_data, 'count': len(calls_data)})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/search-calls')
@with_db_retry
def search_calls():
    try:
        query = request.args.get('q', '').strip()
        if not query:
            return jsonify({'success': False, 'message': 'Search query required'}), 400
        
        calls = Call.query.filter(
            db.or_(
                Call.customer_name.ilike(f'%{query}%'),
                Call.from_number.like(f'%{query}%'),
                Call.postcode.ilike(f'%{query}%'),
                Call.call_sid.ilike(f'%{query}%'),
                Call.customer_email.ilike(f'%{query}%')
            )
        ).order_by(Call.start_time.desc()).limit(50).all()
        
        calls_data = [call.to_dict() for call in calls]
        
        return jsonify({'success': True, 'calls': calls_data, 'count': len(calls_data)})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/update-team-notes', methods=['POST'])
@with_db_retry
def update_team_notes():
    try:
        data = request.get_json()
        call_id = data.get('call_id')
        team_notes = data.get('team_notes', '').strip()
        
        if not call_id:
            return jsonify({'success': False, 'message': 'Call ID required'}), 400
        
        call = Call.query.get(call_id)
        if not call:
            return jsonify({'success': False, 'message': 'Call not found'}), 404
        
        call.team_notes = team_notes
        db.session.commit()
        
        return jsonify({
            'success': True, 
            'message': 'Team notes updated',
            'team_notes': team_notes
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/update-call-status', methods=['POST'])
@with_db_retry
def update_call_status():
    try:
        data = request.get_json()
        call_id = data.get('call_id')
        new_status = data.get('status')
        
        if not call_id or not new_status:
            return jsonify({'success': False, 'message': 'Call ID and status required'}), 400
        
        valid_statuses = ['active', 'ended', 'completed', 'callback']
        if new_status not in valid_statuses:
            return jsonify({'success': False, 'message': 'Invalid status'}), 400
        
        call = Call.query.get(call_id)
        if not call:
            return jsonify({'success': False, 'message': 'Call not found'}), 404
        
        call.status = new_status
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'Status updated to {new_status}',
            'status': new_status
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/download-audio/<int:call_id>', methods=['POST'])
@with_db_retry
def download_audio(call_id):
    try:
        call = Call.query.get(call_id)
        if not call or not call.recording_url:
            return jsonify({'success': False, 'message': 'Audio not available'}), 404
        
        audio_response = download_twilio_audio(call.recording_url, call.recording_sid)
        
        if audio_response['success']:
            call.local_audio_path = audio_response['local_path']
            call.audio_status = 'downloaded'
            db.session.commit()
            
            return jsonify({
                'success': True,
                'message': 'Audio downloaded',
                'local_path': audio_response['local_path']
            })
        else:
            return jsonify({'success': False, 'message': audio_response['message']}), 500
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/audio/<int:call_id>')
@with_db_retry
def get_call_audio(call_id):
    try:
        call = Call.query.get(call_id)
        if not call:
            return jsonify({'success': False, 'message': 'Call not found'}), 404
        
        if call.local_audio_path and os.path.exists(call.local_audio_path):
            return send_file(call.local_audio_path, as_attachment=False)
        elif call.recording_url:
            return jsonify({
                'success': True,
                'audio_url': call.recording_url,
                'duration': call.recording_duration
            })
        else:
            return jsonify({'success': False, 'message': 'No audio available'}), 404
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/stats')
@with_db_retry
def get_stats():
    try:
        total_calls = Call.query.count()
        today_calls = Call.query.filter(
            db.func.date(Call.start_time) == datetime.today().date()
        ).count()
        
        stats = {
            'total_calls': total_calls,
            'today_calls': today_calls,
            'skip_bookings': Call.query.filter_by(booking_confirmed=True).count(),
            'callbacks': Call.query.filter_by(callback_requested=True).count(),
            'trade_customers': Call.query.filter_by(trade_customer=True).count(),
            'active_calls': Call.query.filter_by(status='active').count()
        }
        return jsonify(stats)
    except Exception as e:
        print(f"Error fetching stats: {e}")
        return jsonify({
            'total_calls': 0,
            'today_calls': 0,
            'skip_bookings': 0,
            'callbacks': 0,
            'trade_customers': 0,
            'active_calls': 0
        })

# DASHBOARD 1: Main Dashboard - / route
@app.route('/')
def index():
    try:
        calls = Call.query.order_by(Call.start_time.desc()).limit(100).all()
        calls_data = [call.to_dict() for call in calls]
    except:
        calls_data = []
    
    return render_template_string('''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WasteKing Voice - Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {  
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: #333;
            min-height: 100vh;
            padding: 20px;
        }
        .container { max-width: 1800px; margin: 0 auto; }
        .header {
            background: white;
            border-radius: 10px;
            padding: 30px;
            margin-bottom: 30px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.1);
            position: relative;
        }
        h1 { color: #333; font-size: 2rem; margin-bottom: 20px; }
        .search-container {
            margin-bottom: 20px;
            display: flex;
            gap: 10px;
        }
        .search-input {
            flex: 1;
            padding: 12px 16px;
            border: 2px solid #dee2e6;
            border-radius: 25px;
            font-size: 1rem;
        }
        .search-btn {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 12px 20px;
            border-radius: 25px;
            cursor: pointer;
            font-weight: 600;
        }
        .calls-table {
            background: white;
            border-radius: 10px;
            overflow: hidden;
            box-shadow: 0 10px 30px rgba(0,0,0,0.1);
        }
        .call-group {
            border-bottom: 2px solid #e9ecef;
            transition: all 0.3s ease;
        }
        .call-group:nth-child(odd) { background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%); }
        .call-group:nth-child(even) { background: linear-gradient(135deg, #e3f2fd 0%, #bbdefb 100%); }
        .call-summary-row {
            display: flex;
            align-items: center;
            padding: 15px 20px;
            cursor: pointer;
        }
        .expand-icon {
            font-size: 1.2rem;
            margin-right: 15px;
            transition: transform 0.3s ease;
            color: #667eea;
            font-weight: bold;
        }
        .call-group.expanded .expand-icon { transform: rotate(90deg); }
        .call-info {
            display: grid;
            grid-template-columns: 120px 150px 200px 150px 150px 1fr 150px;
            gap: 15px;
            align-items: center;
            flex: 1;
        }
        .call-details {
            display: none;
            padding: 20px;
            background: rgba(255,255,255,0.9);
            border-top: 1px solid #dee2e6;
        }
        .call-group.expanded .call-details { display: block; }
        
        .details-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 30px;
            margin-bottom: 20px;
        }
        
        .detail-section {
            background: #f8f9fa;
            padding: 15px;
            border-radius: 8px;
        }
        
        .detail-title {
            font-weight: 600;
            color: #333;
            margin-bottom: 10px;
            border-bottom: 1px solid #dee2e6;
            padding-bottom: 5px;
        }
        
        .summary-section {
            grid-column: 1 / -1;
            background: #fff3e0;
            padding: 15px;
            border-radius: 8px;
            border-left: 4px solid #ff9800;
        }
        
        .audio-controls {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 15px;
            border-radius: 8px;
            margin: 15px 0;
        }
        .audio-btn {
            background: rgba(255,255,255,0.2);
            color: white;
            border: 1px solid rgba(255,255,255,0.3);
            padding: 8px 15px;
            border-radius: 20px;
            cursor: pointer;
            margin-right: 10px;
        }
        .audio-btn:disabled { opacity: 0.5; cursor: not-allowed; }
        .dashboard-link {
            background: #e53e3e;
            color: white;
            padding: 10px 20px;
            border-radius: 8px;
            text-decoration: none;
            font-weight: bold;
            position: absolute;
            top: 30px;
            right: 30px;
        }
        .btn {
            padding: 8px 16px;
            border-radius: 6px;
            border: none;
            cursor: pointer;
            font-size: 0.9rem;
            font-weight: 600;
            margin-right: 10px;
        }
        .btn-details { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; }
        .btn-notes { background: linear-gradient(135deg, #28a745 0%, #20c997 100%); color: white; }
        .live-calls-badge {
            position: absolute;
            top: 30px;
            right: 200px;
            background: #38a169;
            color: white;
            padding: 10px 20px;
            border-radius: 8px;
            font-weight: bold;
        }
        
        .modal {
            display: none;
            position: fixed;
            z-index: 1000;
            left: 0;
            top: 0;
            width: 100%;
            height: 100%;
            background-color: rgba(0,0,0,0.5);
        }
        
        .modal-content {
            background-color: white;
            margin: 3% auto;
            padding: 30px;
            border-radius: 10px;
            width: 85%;
            max-width: 900px;
            max-height: 85vh;
            overflow-y: auto;
        }
        
        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            border-bottom: 2px solid #667eea;
            padding-bottom: 10px;
        }
        
        .close {
            color: #aaa;
            font-size: 28px;
            font-weight: bold;
            cursor: pointer;
        }
        
        .close:hover { color: #000; }
        
        .transcript-preview {
            background: #f8f9fa;
            padding: 15px;
            border-radius: 8px;
            max-height: 200px;
            overflow-y: auto;
            margin-top: 15px;
        }
        
        .message {
            margin-bottom: 15px;
            padding: 15px;
            border-radius: 12px;
        }
        
        .message.customer {
            background: #e3f2fd;
            margin-left: 20px;
            border-left: 4px solid #2196F3;
        }
        
        .message.ai_agent {
            background: #e8f5e8;
            margin-right: 20px;
            border-left: 4px solid #4caf50;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>WasteKing Voice Agent Dashboard</h1>
            <div class="live-calls-badge" id="liveCalls">Live: 0</div>
            <a href="/dashboard" class="dashboard-link">Full Dashboard →</a>
            
            <div class="search-container">
                <input type="text" class="search-input" id="searchInput" placeholder="Search by name, phone, postcode...">
                <button class="search-btn" onclick="performSearch()">Search</button>
                <button class="search-btn" onclick="clearSearch()" style="background: #6c757d;">Clear</button>
            </div>
        </div>
        
        <div class="calls-table" id="callsContainer">
            {% for call in calls %}
            <div class="call-group" data-call-id="{{ call.id }}">
                <div class="call-summary-row" onclick="toggleCallExpansion({{ call.id }})">
                    <span class="expand-icon">▶</span>
                    <div class="call-info">
                        <div>{{ call.call_sid[-8:] }}</div>
                        <div>{{ call.timestamp or call.start_time }}</div>
                        <div><strong>{{ call.customer_name or 'Unknown' }}</strong></div>
                        <div>{{ call.postcode or 'N/A' }}</div>
                        <div>{{ call.phone or call.from or 'Unknown' }}</div>
                        <div>{{ call.service or 'N/A' }}</div>
                        <div>{{ call.status }}</div>
                    </div>
                </div>
                
                <div class="call-details">
                    <div class="details-grid">
                        <div class="detail-section">
                            <div class="detail-title">Customer Information</div>
                            <p><strong>Name:</strong> {{ call.customer_name or 'Unknown' }}</p>
                            <p><strong>Phone:</strong> {{ call.phone or call.from or 'Unknown' }}</p>
                            <p><strong>Email:</strong> {{ call.email or 'Not provided' }}</p>
                            <p><strong>Address:</strong> {{ call.address or 'Not provided' }}</p>
                            <p><strong>Postcode:</strong> {{ call.postcode or 'Unknown' }}</p>
                        </div>
                        
                        <div class="detail-section">
                            <div class="detail-title">Service Details</div>
                            <p><strong>Service:</strong> {{ call.service or 'Unknown' }}</p>
                            <p><strong>Skip Size:</strong> {{ call.skip_size or 'N/A' }}</p>
                            <p><strong>Waste Type:</strong> {{ call.waste_type or 'N/A' }}</p>
                            <p><strong>Duration:</strong> {{ call.recording_duration or 0 }} seconds</p>
                        </div>
                    </div>
                    
                    <div class="summary-section">
                        <div class="detail-title">Call Summary</div>
                        <p>{{ call.customer_name or 'Unknown' }}, a {{ 'trade' if call.trade_customer else 'domestic' }} customer, contacted Waste King for {{ call.service or 'service' }}.</p>
                        {% if call.team_notes %}
                            <div style="margin-top: 10px; padding-top: 10px; border-top: 1px solid #dee2e6;">
                                <strong>Team Notes:</strong> {{ call.team_notes }}
                            </div>
                        {% endif %}
                    </div>
                    
                    {% if call.recording_url %}
                    <div class="audio-controls">
                        <strong>Call Recording</strong> ({{ call.recording_duration }}s) - Status: {{ call.audio_status }}
                        <div style="margin-top: 10px;">
                            <button class="audio-btn" onclick="event.stopPropagation(); playAudio({{ call.id }})">▶ Play</button>
                            <button class="audio-btn" onclick="event.stopPropagation(); pauseAudio({{ call.id }})" disabled id="pause-{{ call.id }}">⏸ Pause</button>
                            <button class="audio-btn" onclick="event.stopPropagation(); stopAudio({{ call.id }})" disabled id="stop-{{ call.id }}">⏹ Stop</button>
                            {% if call.audio_status != 'downloaded' %}
                            <button class="audio-btn" onclick="event.stopPropagation(); downloadAudio({{ call.id }})">⬇ Download</button>
                            {% endif %}
                        </div>
                        <audio id="audio-{{ call.id }}" style="width: 100%; margin-top: 10px;" controls>
                            <source src="{{ call.recording_url }}" type="audio/mpeg">
                        </audio>
                    </div>
                    {% else %}
                    <div class="audio-controls">
                        <strong>Call Recording</strong>
                        <div style="margin-top: 10px;">Checking for audio recording...</div>
                        <button class="audio-btn" onclick="event.stopPropagation(); tryLoadAudio({{ call.id }})">Try Load Audio</button>
                    </div>
                    {% endif %}
                    
                    <div class="transcript-preview">
                        <div class="detail-title">Transcript Preview</div>
                        <div id="transcript-preview-{{ call.id }}" style="max-height: 150px; overflow-y: auto; font-size: 0.9rem;">
                            Loading transcript...
                        </div>
                    </div>
                    
                    <div style="margin-top: 15px;">
                        <button class="btn btn-details" onclick="event.stopPropagation(); showFullDetails({{ call.id }})">Full Transcript</button>
                        <button class="btn btn-notes" onclick="event.stopPropagation(); addTeamNotes({{ call.id }})">Edit Notes</button>
                        <button class="btn btn-details" onclick="event.stopPropagation(); updateCallStatus({{ call.id }})">Update Status</button>
                    </div>
                </div>
            </div>
            {% endfor %}
        </div>
    </div>
    
    <div id="detailsModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h2>Full Call Details & Transcript</h2>
                <span class="close" onclick="closeModal()">&times;</span>
            </div>
            <div id="modalDetails"></div>
        </div>
    </div>
    
    <script>
        const calls = {{ calls | tojson | safe }};
        
        function toggleCallExpansion(callId) {
            const callGroup = document.querySelector(`[data-call-id="${callId}"]`);
            callGroup.classList.toggle('expanded');
            
            const call = calls.find(c => c.id === callId);
            if (call && call.transcript) {
                const previewEl = document.getElementById(`transcript-preview-${callId}`);
                if (call.transcript.length > 0) {
                    previewEl.innerHTML = call.transcript.slice(0, 3).map(t => 
                        `<strong>${t.speaker}:</strong> ${t.text}`
                    ).join('<br><br>');
                } else {
                    previewEl.innerHTML = 'No transcript available yet';
                }
            }
        }
        
        function playAudio(id) {
            const audio = document.getElementById(`audio-${id}`);
            audio.play();
            document.getElementById(`pause-${id}`).disabled = false;
            document.getElementById(`stop-${id}`).disabled = false;
        }
        
        function pauseAudio(id) {
            const audio = document.getElementById(`audio-${id}`);
            audio.pause();
        }
        
        function stopAudio(id) {
            const audio = document.getElementById(`audio-${id}`);
            audio.pause();
            audio.currentTime = 0;
            document.getElementById(`pause-${id}`).disabled = true;
            document.getElementById(`stop-${id}`).disabled = true;
        }
        
        async function tryLoadAudio(id) {
            try {
                const response = await fetch(`/api/audio/${id}`);
                const data = await response.json();
                if (data.success) {
                    location.reload();
                } else {
                    alert('No audio available yet');
                }
            } catch (error) {
                alert('Failed to load audio');
            }
        }
        
        async function downloadAudio(id) {
            try {
                const response = await fetch(`/api/download-audio/${id}`, { method: 'POST' });
                const data = await response.json();
                if (data.success) {
                    alert('Audio downloaded successfully!');
                    location.reload();
                }
            } catch (error) {
                alert('Failed to download audio');
            }
        }
        
        function showFullDetails(callId) {
            const call = calls.find(c => c.id === callId);
            if (call) {
                const modal = document.getElementById('detailsModal');
                const details = document.getElementById('modalDetails');
                
                let transcriptHtml = '';
                if (call.transcript && call.transcript.length > 0) {
                    transcriptHtml = call.transcript.map(msg => `
                        <div class="message ${msg.speaker.toLowerCase()}">
                            <strong>${msg.speaker}</strong>
                            <span style="float: right; font-size: 12px; opacity: 0.7;">${msg.time || ''}</span>
                            <div style="clear: both; margin-top: 5px;">${msg.text}</div>
                        </div>
                    `).join('');
                } else {
                    transcriptHtml = '<p>No transcript available</p>';
                }
                
                details.innerHTML = `
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px;">
                        <div>
                            <p><strong>Call ID:</strong> ${call.call_sid.substr(-8)}</p>
                            <p><strong>Customer:</strong> ${call.customer_name || 'Unknown'}</p>
                            <p><strong>Phone:</strong> ${call.phone || 'Unknown'}</p>
                            <p><strong>Email:</strong> ${call.email || 'Not provided'}</p>
                            <p><strong>Address:</strong> ${call.address || 'Not provided'}</p>
                            <p><strong>Postcode:</strong> ${call.postcode || 'Unknown'}</p>
                        </div>
                        <div>
                            <p><strong>Service:</strong> ${call.service || 'Unknown'}</p>
                            <p><strong>Duration:</strong> ${call.recording_duration || 0} seconds</p>
                            <p><strong>Status:</strong> ${call.status}</p>
                            <p><strong>Trade Customer:</strong> ${call.trade_customer ? 'Yes' : 'No'}</p>
                        </div>
                    </div>
                    
                    <div style="background: #f8f9fa; padding: 15px; border-radius: 8px; margin: 20px 0;">
                        <h4>Full Transcript:</h4>
                        <div style="max-height: 400px; overflow-y: auto;">
                            ${transcriptHtml}
                        </div>
                    </div>
                `;
                
                modal.style.display = 'block';
            }
        }
        
        function closeModal() {
            document.getElementById('detailsModal').style.display = 'none';
        }
        
        async function performSearch() {
            const query = document.getElementById('searchInput').value;
            if (!query) return;
            
            const response = await fetch(`/api/search-calls?q=${encodeURIComponent(query)}`);
            const data = await response.json();
            if (data.success) {
                location.href = `/?search=${encodeURIComponent(query)}`;
            }
        }
        
        function clearSearch() {
            document.getElementById('searchInput').value = '';
            location.href = '/';
        }
        
        function addTeamNotes(id) {
            const call = calls.find(c => c.id === id);
            const notes = prompt('Enter team notes:', call.team_notes || '');
            if (notes !== null) {
                fetch('/api/update-team-notes', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({call_id: id, team_notes: notes})
                }).then(() => location.reload());
            }
        }
        
        function updateCallStatus(id) {
            const status = prompt('Enter new status (active/ended/completed/callback):');
            if (status) {
                fetch('/api/update-call-status', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({call_id: id, status: status})
                }).then(() => location.reload());
            }
        }
        
        async function loadLiveCalls() {
            try {
                const response = await fetch('/api/live-calls');
                const data = await response.json();
                document.getElementById('liveCalls').textContent = `Live: ${data.count || 0}`;
            } catch (error) {
                console.error('Error loading live calls:', error);
            }
        }
        
        loadLiveCalls();
        setInterval(loadLiveCalls, 5000);
        
        window.onclick = function(event) {
            const modal = document.getElementById('detailsModal');
            if (event.target == modal) {
                closeModal();
            }
        }
    </script>
</body>
</html>
    ''', calls=calls_data)

# DASHBOARD 2: Full Dashboard - /dashboard route
@app.route('/dashboard')
def dashboard():
    return render_template_string('''
<!DOCTYPE html>
<html>
<head>
    <title>WasteKing Voice Agent - Full Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {  
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: #333;
            min-height: 100vh;
            padding: 20px;
        }
        .container { max-width: 1400px; margin: 0 auto; }
        .header {
            background: white;
            padding: 30px;
            border-radius: 20px;
            margin-bottom: 20px;
            box-shadow: 0 8px 32px rgba(0,0,0,0.1);
        }
        h1 { font-size: 32px; font-weight: 600; margin-bottom: 20px; }
        .tabs { display: flex; gap: 10px; flex-wrap: wrap; }
        .tab {
            padding: 12px 20px;
            background: #f0f0f0;
            border: none;
            border-radius: 25px;
            cursor: pointer;
            font-weight: 500;
        }
        .tab.active { background: #e53e3e; color: white; }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .stat-card {
            background: white;
            padding: 25px;
            border-radius: 15px;
            text-align: center;
            box-shadow: 0 8px 32px rgba(0,0,0,0.1);
        }
        .stat-number { font-size: 36px; font-weight: bold; color: #e53e3e; }
        .dashboard-content {
            background: white;
            border-radius: 20px;
            overflow: hidden;
            box-shadow: 0 8px 32px rgba(0,0,0,0.1);
        }
        .table-header {
            background: linear-gradient(135deg, #e53e3e, #c53030);
            color: white;
            padding: 20px;
            display: grid;
            grid-template-columns: 80px 150px 120px 150px 150px 100px 80px 120px;
            gap: 15px;
            font-weight: 600;
            font-size: 12px;
        }
        .call-row {
            padding: 20px;
            display: grid;
            grid-template-columns: 80px 150px 120px 150px 150px 100px 80px 120px;
            gap: 15px;
            border-bottom: 1px solid #f0f0f0;
        }
        .call-row:hover { background: #fef5f5; }
        .btn-details {
            background: #e53e3e;
            color: white;
            padding: 6px 12px;
            border: none;
            border-radius: 6px;
            cursor: pointer;
        }
        .live-link {
            background: #38a169;
            color: white;
            padding: 8px 16px;
            border-radius: 8px;
            text-decoration: none;
            font-weight: 600;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <div>
                    <h1>WasteKing Voice Agent - Full Dashboard</h1>
                    <div class="tabs">
                        <div class="tab active" onclick="setActiveTab('all', this)">All Calls</div>
                        <div class="tab" onclick="setActiveTab('skip-hire', this)">Skip Hire</div>
                        <div class="tab" onclick="setActiveTab('man-van', this)">Man & Van</div>
                        <div class="tab" onclick="setActiveTab('trade', this)">Trade</div>
                        <div class="tab" onclick="setActiveTab('callbacks', this)">Callbacks</div>
                    </div>
                </div>
                <a href="/" class="live-link">← Main Dashboard</a>
            </div>
        </div>
        
        <div class="stats-grid">
            <div class="stat-card"><div class="stat-number" id="totalCalls">0</div><div>Total Calls</div></div>
            <div class="stat-card"><div class="stat-number" id="skipHireCalls">0</div><div>Skip Hire Calls</div></div>
            <div class="stat-card"><div class="stat-number" id="tradeCalls">0</div><div>Trade Customers</div></div>
            <div class="stat-card"><div class="stat-number" id="activeCalls">0</div><div>Active Calls</div></div>
        </div>
        
        <div class="dashboard-content">
            <div class="table-header">
                <div>TIME</div><div>CUSTOMER</div><div>POSTCODE</div><div>PHONE</div><div>SERVICE</div><div>STATUS</div><div>MSGS</div><div>ACTIONS</div>
            </div>
            <div id="callsTable"></div>
        </div>
    </div>

    <script>
        let allCalls = [];
        let currentFilter = 'all';

        function setActiveTab(filter, element) {
            document.querySelectorAll('.tab').forEach(tab => tab.classList.remove('active'));
            element.classList.add('active');
            currentFilter = filter;
            renderFilteredCalls();
        }

        function filterCalls(calls, filter) {
            switch(filter) {
                case 'all': return calls;
                case 'skip-hire': return calls.filter(call => call.service === 'Skip Hire');
                case 'man-van': return calls.filter(call => call.service === 'Man & Van');
                case 'trade': return calls.filter(call => call.trade_customer === true);
                case 'callbacks': return calls.filter(call => call.callback_requested === true);
                default: return calls;
            }
        }

        function renderFilteredCalls() {
            const container = document.getElementById('callsTable');
            container.innerHTML = '';
            
            const filteredCalls = filterCalls(allCalls, currentFilter);
            
            filteredCalls.forEach(call => {
                const row = document.createElement('div');
                row.className = 'call-row';
                
                const time = new Date(call.start_time).toLocaleTimeString('en-GB', {
                    hour: '2-digit', 
                    minute: '2-digit'
                });
                
                row.innerHTML = `
                    <div>${time}</div>
                    <div><strong>${call.customer_name || 'Unknown'}</strong></div>
                    <div>${call.postcode || 'N/A'}</div>
                    <div>${call.phone || call.from || 'Unknown'}</div>
                    <div>${call.service || 'Unknown'}</div>
                    <div>${call.status}</div>
                    <div>${call.transcript_count}</div>
                    <div><button class="btn-details">Details</button></div>
                `;
                
                container.appendChild(row);
            });
        }
        
        async function fetchAllData() {
            try {
                const response = await fetch('/api/conversations');
                const data = await response.json();
                allCalls = data.calls;
                
                document.getElementById('totalCalls').textContent = allCalls.length;
                document.getElementById('skipHireCalls').textContent = allCalls.filter(c => c.service === 'Skip Hire').length;
                document.getElementById('tradeCalls').textContent = allCalls.filter(c => c.trade_customer).length;
                document.getElementById('activeCalls').textContent = allCalls.filter(c => c.status === 'active').length;

                renderFilteredCalls();
            } catch (error) {
                console.error('Error:', error);
            }
        }

        fetchAllData();
        setInterval(fetchAllData, 3000);
    </script>
</body>
</html>
    ''')

if __name__ == '__main__':
    cleanup_old_audio_files()
    cleanup_old_database_records()
    
    port = int(os.environ.get('PORT', 8000))
    print(f"Server running on port {port}")
    print(f"Main Dashboard: http://localhost:{port}/")
    print(f"Full Dashboard: http://localhost:{port}/dashboard")
    print("✓ Audio: Public URLs (no auth required)")
    print("✓ Cleanup: Audio > 2hrs, DB > 3 months")
    app.run(host='0.0.0.0', port=port)
