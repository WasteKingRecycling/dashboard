"""
WasteKing Voice Agent - Complete System
Single file with Live Calls + Full Dashboard + Call Recording
"""

from flask import Flask, request, jsonify, render_template_string, send_file
from flask_sqlalchemy import SQLAlchemy
from twilio.twiml.voice_response import VoiceResponse, Start, Dial
from twilio.rest import Client
import os
import json
import requests
import re
from datetime import datetime, timedelta
from pytz import timezone

app = Flask(__name__)

# Configuration
DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://localhost/wasteking_voice')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_size': 10,
    'pool_recycle': 280,
    'pool_pre_ping': True,
}

db = SQLAlchemy(app)

# Environment Variables
ELEVENLABS_PHONE_NUMBER = os.environ.get('ELEVENLABS_PHONE_NUMBER', '+447366432353')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')

# UK Timezone
UK_TZ = timezone('Europe/London')

# Database Models
class Call(db.Model):
    __tablename__ = 'calls'
    
    id = db.Column(db.Integer, primary_key=True)
    unique_call_id = db.Column(db.String(20), unique=True, nullable=False, index=True)
    call_sid = db.Column(db.String(100), unique=True, nullable=False, index=True)
    from_number = db.Column(db.String(50))
    start_time = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(20), default='active')
    
    # Customer Info
    customer_name = db.Column(db.String(100))
    postcode = db.Column(db.String(20))
    service = db.Column(db.String(200))
    customer_address = db.Column(db.Text)
    customer_email = db.Column(db.String(100))
    
    # Call Details
    callback_requested = db.Column(db.Boolean, default=False)
    trade_customer = db.Column(db.Boolean, default=False)
    complaint = db.Column(db.Boolean, default=False)
    quote_provided = db.Column(db.Boolean, default=False)
    booking_confirmed = db.Column(db.Boolean, default=False)
    payment_link_sent = db.Column(db.Boolean, default=False)
    
    # Status Fields
    call_status = db.Column(db.String(50), default='completed')  # completed, live_call_agent, live_call_team, ticket_raised
    
    # Service Specific
    skip_size = db.Column(db.String(50))
    waste_type = db.Column(db.String(200))
    when_needed = db.Column(db.String(100))
    
    # Notes
    team_notes = db.Column(db.Text)
    
    # Recording
    recording_sid = db.Column(db.String(100))
    recording_url = db.Column(db.String(500))
    recording_duration = db.Column(db.Integer, default=0)
    
    transcripts = db.relationship('Transcript', backref='call', lazy=True, cascade='all, delete-orphan')
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not self.unique_call_id:
            import random
            timestamp = datetime.now().strftime('%y%m%d%H%M')
            suffix = str(random.randint(100, 999))
            self.unique_call_id = f"WK{timestamp}{suffix}"
    
    def get_uk_time(self):
        """Convert UTC time to UK time"""
        if self.start_time:
            utc_time = self.start_time.replace(tzinfo=timezone('UTC'))
            return utc_time.astimezone(UK_TZ)
        return None
    
    def to_dict(self):
        uk_time = self.get_uk_time()
        return {
            'id': self.id,
            'unique_call_id': self.unique_call_id,
            'call_sid': self.call_sid,
            'from': self.from_number,
            'start_time': uk_time.isoformat() if uk_time else None,
            'time': uk_time.strftime('%H:%M') if uk_time else None,
            'date': uk_time.strftime('%d/%m/%Y') if uk_time else None,
            'status': self.status,
            'call_status': self.call_status,
            'customer_name': self.customer_name,
            'postcode': self.postcode,
            'service': self.service,
            'customer_address': self.customer_address,
            'customer_email': self.customer_email,
            'callback_requested': self.callback_requested,
            'trade_customer': self.trade_customer,
            'complaint': self.complaint,
            'quote_provided': self.quote_provided,
            'booking_confirmed': self.booking_confirmed,
            'payment_link_sent': self.payment_link_sent,
            'skip_size': self.skip_size,
            'waste_type': self.waste_type,
            'when_needed': self.when_needed,
            'team_notes': self.team_notes or '',
            'transcript_count': len(self.transcripts),
            'has_recording': bool(self.recording_url),
            'recording_url': self.recording_url,
            'recording_duration': self.recording_duration
        }

class Transcript(db.Model):
    __tablename__ = 'transcripts'
    
    id = db.Column(db.Integer, primary_key=True)
    call_sid = db.Column(db.String(100), db.ForeignKey('calls.call_sid'), nullable=False, index=True)
    speaker = db.Column(db.String(20))
    text = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    
    def get_uk_time(self):
        if self.timestamp:
            utc_time = self.timestamp.replace(tzinfo=timezone('UTC'))
            return utc_time.astimezone(UK_TZ)
        return None
    
    def to_dict(self):
        uk_time = self.get_uk_time()
        return {
            'speaker': self.speaker,
            'text': self.text,
            'timestamp': uk_time.isoformat() if uk_time else None,
            'time': uk_time.strftime('%H:%M:%S') if uk_time else None
        }

# Create tables
with app.app_context():
    db.create_all()
    print("Database tables created")

# OpenAI Extraction
def extract_with_openai(text, call):
    """Extract customer information using OpenAI"""
    if not OPENAI_API_KEY:
        return False
    
    # Get recent conversation context
    recent = db.session.query(Transcript.speaker, Transcript.text).filter(
        Transcript.call_sid == call.call_sid
    ).order_by(Transcript.timestamp.desc()).limit(5).all()
    
    context = [{"role": "user" if t.speaker == 'CUSTOMER' else "assistant", "content": t.text} for t in reversed(recent)]
    context.append({"role": "user", "content": text})
    
    conversation_str = "\n".join([f"{msg['role'].upper()}: {msg['content']}" for msg in context])
    
    prompt = f"""Extract information from this WasteKing call. Return JSON only.

Conversation:
{conversation_str}

Extract EXACTLY:
- customer_name: Full name
- postcode: UK postcode with space (e.g., "LS14 8AB", "LU7 2RC")
- service: One of: "Skip Hire", "Man & Van", "Grab Hire", "RORO", "Toilet Hire", "Wheelie Bins", "Waste Bags", "Road Sweeper"
- trade_customer: true/false
- callback_requested: true/false
- when_needed: When service needed

CRITICAL - Postcode:
- If customer says "LS one four ED" → "LS1 4ED"
- If customer says "LU seven ZRC" → "LU7 2RC"
- Always format with space before last 3 characters

Return JSON:
{{"customer_name": "", "postcode": "", "service": "", "trade_customer": false, "callback_requested": false, "when_needed": ""}}
"""
    
    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 250,
                "temperature": 0.1,
                "response_format": {"type": "json_object"}
            },
            timeout=10
        )
        
        if response.status_code == 200:
            result = response.json()
            extracted = json.loads(result['choices'][0]['message']['content'])
            
            updated = False
            for field, value in extracted.items():
                if hasattr(call, field) and value and value != "":
                    current = getattr(call, field)
                    if not current or current == "":
                        setattr(call, field, value)
                        updated = True
                        print(f"EXTRACTED {field}: {value}")
            
            return updated
    except Exception as e:
        print(f"OpenAI error: {e}")
    
    return False

# Twilio Routes
@app.route('/voice/incoming', methods=['POST', 'GET'])
def handle_incoming_call():
    """Handle incoming call - enable recording"""
    call_sid = request.form.get('CallSid')
    from_number = request.form.get('From')
    
    try:
        call = Call.query.filter_by(call_sid=call_sid).first()
        if not call:
            call = Call(call_sid=call_sid, from_number=from_number, status='active')
            db.session.add(call)
            db.session.commit()
            print(f"New call created: {call.unique_call_id} from {from_number}")
    except Exception as e:
        db.session.rollback()
        print(f"Database error: {e}")
    
    response = VoiceResponse()
    
    # Enable transcription
    start = Start()
    transcription = start.transcription(
        statusCallbackUrl=f'https://{request.host}/voice/transcription',
        track='both_tracks',
        partialResults=True,
        languageCode='en-US'
    )
    response.append(start)
    response.pause(length=1)
    
    # Enable recording
    dial = Dial(
        timeout=30,
        hangupOnStar=False,
        record='record-from-answer',
        recordingStatusCallback=f'https://{request.host}/voice/recording-callback',
        recordingStatusCallbackMethod='POST'
    )
    dial.number(ELEVENLABS_PHONE_NUMBER)
    response.append(dial)
    
    response.say("Sorry, we're unable to connect you at the moment.", voice='alice')
    
    return str(response)

@app.route('/voice/recording-callback', methods=['POST'])
def recording_callback():
    """Handle Twilio recording completion"""
    call_sid = request.form.get('CallSid')
    recording_sid = request.form.get('RecordingSid')
    recording_url = request.form.get('RecordingUrl')
    recording_duration = request.form.get('RecordingDuration', 0)
    
    print(f"Recording ready: {recording_sid} for call {call_sid}")
    
    try:
        call = Call.query.filter_by(call_sid=call_sid).first()
        if call:
            call.recording_sid = recording_sid
            call.recording_url = f"{recording_url}.mp3"
            call.recording_duration = int(recording_duration)
            db.session.commit()
            print(f"Recording saved for call {call.unique_call_id}")
    except Exception as e:
        db.session.rollback()
        print(f"Error saving recording: {e}")
    
    return "OK", 200

@app.route('/voice/transcription', methods=['POST'])
def handle_transcription():
    """Handle transcription events"""
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
                        extract_with_openai(text, call)
                    
                    db.session.commit()
            except Exception as e:
                db.session.rollback()
                print(f"Transcription error: {e}")
    
    elif event == 'transcription-stopped':
        try:
            call = Call.query.filter_by(call_sid=call_sid).first()
            if call:
                call.status = 'ended'
                db.session.commit()
        except Exception as e:
            db.session.rollback()
    
    return "OK", 200

# API Routes
@app.route('/api/conversations')
def get_conversations():
    """Get all recent calls"""
    try:
        two_hours_ago = datetime.utcnow() - timedelta(hours=2)
        calls = Call.query.filter(Call.start_time >= two_hours_ago).order_by(Call.start_time.desc()).all()
        return jsonify({'calls': [call.to_dict() for call in calls]})
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({'calls': []}), 500

@app.route('/api/conversations/<call_sid>')
def get_conversation(call_sid):
    """Get single call with transcripts"""
    try:
        call = Call.query.filter_by(call_sid=call_sid).first()
        if not call:
            return jsonify({'error': 'Call not found'}), 404
        
        transcripts = Transcript.query.filter_by(call_sid=call_sid).order_by(Transcript.timestamp).all()
        
        return jsonify({
            'call_info': call.to_dict(),
            'transcripts': [t.to_dict() for t in transcripts]
        })
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/update-team-notes', methods=['POST'])
def update_team_notes():
    """Update team notes"""
    try:
        data = request.json
        call_id = data.get('call_id')
        team_notes = data.get('team_notes', '').strip()
        
        call = Call.query.get(call_id)
        if not call:
            return jsonify({'success': False, 'message': 'Call not found'}), 404
        
        call.team_notes = team_notes
        db.session.commit()
        
        return jsonify({'success': True, 'team_notes': team_notes})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/update-status', methods=['POST'])
def update_status():
    """Update call status"""
    try:
        data = request.json
        call_id = data.get('call_id')
        new_status = data.get('status')
        
        valid_statuses = ['completed', 'live_call_agent', 'live_call_team', 'ticket_raised']
        if new_status not in valid_statuses:
            return jsonify({'success': False, 'message': 'Invalid status'}), 400
        
        call = Call.query.get(call_id)
        if not call:
            return jsonify({'success': False, 'message': 'Call not found'}), 404
        
        call.call_status = new_status
        db.session.commit()
        
        return jsonify({'success': True, 'status': new_status})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/audio/<int:call_id>')
def get_audio(call_id):
    """Get audio recording"""
    try:
        call = Call.query.get(call_id)
        if not call or not call.recording_url:
            return jsonify({'success': False, 'message': 'No recording'}), 404
        
        return jsonify({
            'success': True,
            'audio_url': call.recording_url,
            'duration': call.recording_duration
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/stats')
def get_stats():
    """Get dashboard statistics"""
    try:
        total = Call.query.count()
        today = Call.query.filter(
            db.func.date(Call.start_time) == datetime.today().date()
        ).count()
        callbacks = Call.query.filter_by(callback_requested=True).count()
        complaints = Call.query.filter_by(complaint=True).count()
        active = Call.query.filter_by(status='active').count()
        
        return jsonify({
            'total_calls': total,
            'today_calls': today,
            'callbacks': callbacks,
            'complaints': complaints,
            'active_calls': active
        })
    except Exception as e:
        print(f"Stats error: {e}")
        return jsonify({
            'total_calls': 0,
            'today_calls': 0,
            'callbacks': 0,
            'complaints': 0,
            'active_calls': 0
        })

# Dashboard Routes
@app.route('/')
def live_calls():
    """Live Calls Dashboard - Auto-refresh every 3 seconds"""
    return render_template_string(LIVE_CALLS_TEMPLATE)

@app.route('/dashboard')
def full_dashboard():
    """Full Dashboard - Manual refresh, no auto"""
    return render_template_string(FULL_DASHBOARD_TEMPLATE)

# HTML Templates
LIVE_CALLS_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>WasteKing Voice - Live Calls</title>
    <meta http-equiv="refresh" content="3">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        
        .header {
            background: white;
            padding: 25px 30px;
            border-radius: 15px;
            margin-bottom: 25px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.15);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .header h1 { font-size: 28px; color: #333; }
        
        .logo-container {
            display: flex;
            align-items: center;
            gap: 15px;
        }
        
        .logo-box {
            width: 50px;
            height: 50px;
            background: #e53e3e;
            border-radius: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
            font-weight: 900;
            font-size: 18px;
        }
        
        .logo-text { display: flex; flex-direction: column; }
        .brand-name { font-size: 20px; font-weight: 700; color: #333; }
        .tagline { font-size: 11px; color: #666; }
        
        .full-dashboard-btn {
            background: linear-gradient(135deg, #e53e3e, #c53030);
            color: white;
            padding: 10px 24px;
            border-radius: 8px;
            text-decoration: none;
            font-weight: 600;
            font-size: 14px;
            transition: transform 0.2s;
        }
        
        .full-dashboard-btn:hover { transform: translateY(-2px); }
        
        .calls-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(400px, 1fr));
            gap: 20px;
        }
        
        .call-card {
            background: white;
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 8px 20px rgba(0,0,0,0.1);
        }
        
        .call-card.active {
            border: 3px solid #48bb78;
            box-shadow: 0 0 0 3px rgba(72,187,120,0.2);
        }
        
        .call-header {
            display: flex;
            justify-content: space-between;
            margin-bottom: 15px;
            padding-bottom: 15px;
            border-bottom: 2px solid #f0f0f0;
        }
        
        .call-id {
            font-family: 'Courier New', monospace;
            font-weight: bold;
            color: #e53e3e;
            font-size: 16px;
        }
        
        .status-badge {
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 11px;
            font-weight: bold;
            text-transform: uppercase;
        }
        
        .status-badge.active { background: #d4edda; color: #155724; }
        .status-badge.ended { background: #f8d7da; color: #721c24; }
        
        .call-info { margin-bottom: 10px; font-size: 14px; color: #555; }
        .call-info strong { color: #333; }
        
        .transcript-box {
            background: #f8f9fa;
            border-radius: 8px;
            padding: 15px;
            max-height: 300px;
            overflow-y: auto;
            margin-top: 15px;
        }
        
        .transcript-item {
            margin-bottom: 12px;
            padding: 10px;
            border-radius: 6px;
            font-size: 13px;
        }
        
        .transcript-item.CUSTOMER {
            background: #e3f2fd;
            border-left: 3px solid #2196F3;
        }
        
        .transcript-item.AI_AGENT {
            background: #e8f5e8;
            border-left: 3px solid #4caf50;
        }
        
        .no-calls {
            text-align: center;
            padding: 60px;
            background: white;
            border-radius: 15px;
            color: #999;
            font-size: 18px;
        }
    </style>
</head>
<body>
    <div class="header">
        <div>
            <h1>Live Customer Calls</h1>
            <p style="color: #666; font-size: 14px; margin-top: 5px;">Auto-refresh every 3 seconds</p>
        </div>
        <div style="display: flex; align-items: center; gap: 20px;">
            <a href="/dashboard" class="full-dashboard-btn">Full Dashboard →</a>
            <div class="logo-container">
                <div class="logo-box">WK</div>
                <div class="logo-text">
                    <div class="brand-name">WasteKing</div>
                    <div class="tagline">Waste Management Solutions</div>
                </div>
            </div>
        </div>
    </div>
    
    <div class="calls-grid" id="callsGrid"></div>

    <script>
        async function loadCalls() {
            try {
                const response = await fetch('/api/conversations');
                const data = await response.json();
                const container = document.getElementById('callsGrid');
                
                if (!data.calls || data.calls.length === 0) {
                    container.innerHTML = '<div class="no-calls">No active calls at the moment</div>';
                    return;
                }
                
                container.innerHTML = '';
                
                for (const call of data.calls) {
                    const detailResponse = await fetch(`/api/conversations/${call.call_sid}`);
                    const details = await detailResponse.json();
                    
                    const card = document.createElement('div');
                    card.className = `call-card ${call.status}`;
                    
                    let transcriptsHtml = '';
                    if (details.transcripts && details.transcripts.length > 0) {
                        for (const t of details.transcripts) {
                            transcriptsHtml += `
                                <div class="transcript-item ${t.speaker}">
                                    <strong>${t.speaker}:</strong> ${t.text}
                                    <div style="font-size: 11px; color: #999; margin-top: 3px;">${t.time}</div>
                                </div>
                            `;
                        }
                    } else {
                        transcriptsHtml = '<div style="text-align:center;color:#999;">No transcript yet</div>';
                    }
                    
                    card.innerHTML = `
                        <div class="call-header">
                            <div class="call-id">${call.unique_call_id}</div>
                            <span class="status-badge ${call.status}">${call.status.toUpperCase()}</span>
                        </div>
                        <div class="call-info"><strong>From:</strong> ${call.from || 'Unknown'}</div>
                        <div class="call-info"><strong>Customer:</strong> ${call.customer_name || 'Not identified'}</div>
                        <div class="call-info"><strong>Time:</strong> ${call.time || '--:--'}</div>
                        <div class="call-info"><strong>Messages:</strong> ${call.transcript_count}</div>
                        <div class="transcript-box">${transcriptsHtml}</div>
                    `;
                    
                    container.appendChild(card);
                }
            } catch (error) {
                console.error('Error loading calls:', error);
            }
        }
        
        loadCalls();
    </script>
</body>
</html>
'''

FULL_DASHBOARD_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>WasteKing Voice Agent - Full Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        
        .header {
            background: white;
            padding: 30px;
            border-radius: 15px;
            margin-bottom: 25px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.15);
        }
        
        .header-top {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }
        
        .header h1 { font-size: 32px; color: #333; margin-bottom: 5px; }
        .header p { color: #666; font-size: 14px; }
        
        .logo-container {
            display: flex;
            align-items: center;
            gap: 15px;
        }
        
        .logo-box {
            width: 60px;
            height: 60px;
            background: #e53e3e;
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
            font-weight: 900;
            font-size: 22px;
        }
        
        .logo-text { display: flex; flex-direction: column; }
        .brand-name { font-size: 24px; font-weight: 700; color: #333; }
        .tagline { font-size: 12px; color: #666; }
        
        .live-calls-btn {
            background: linear-gradient(135deg, #48bb78, #38a169);
            color: white;
            padding: 12px 24px;
            border-radius: 8px;
            text-decoration: none;
            font-weight: 600;
            font-size: 14px;
            transition: transform 0.2s;
        }
        
        .live-calls-btn:hover { transform: translateY(-2px); }
        
        .tabs {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
        }
        
        .tab {
            padding: 10px 20px;
            background: #f0f0f0;
            border: none;
            border-radius: 20px;
            cursor: pointer;
            font-weight: 500;
            transition: all 0.3s;
            color: #666;
            font-size: 14px;
        }
        
        .tab.active {
            background: linear-gradient(135deg, #e53e3e, #c53030);
            color: white;
        }
        
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 25px;
        }
        
        .stat-card {
            background: white;
            padding: 25px;
            border-radius: 12px;
            text-align: center;
            box-shadow: 0 8px 20px rgba(0,0,0,0.1);
        }
        
        .stat-number {
            font-size: 40px;
            font-weight: bold;
            color: #e53e3e;
            margin-bottom: 8px;
        }
        
        .stat-label { font-size: 14px; color: #666; }
        
        .table-container {
            background: white;
            border-radius: 15px;
            overflow: hidden;
            box-shadow: 0 10px 30px rgba(0,0,0,0.15);
        }
        
        .table-header {
            background: linear-gradient(135deg, #e53e3e, #c53030);
            color: white;
            padding: 20px;
            display: grid;
            grid-template-columns: 80px 150px 130px 150px 180px 120px 80px 100px;
            gap: 15px;
            font-weight: 600;
            font-size: 12px;
            text-transform: uppercase;
        }
        
        .table-row {
            padding: 18px 20px;
            display: grid;
            grid-template-columns: 80px 150px 130px 150px 180px 120px 80px 100px;
            gap: 15px;
            border-bottom: 1px solid #f0f0f0;
            cursor: pointer;
            transition: all 0.2s;
            align-items: center;
        }
        
        .table-row:hover {
            background: #fef5f5;
            transform: translateX(5px);
        }
        
        .call-id-cell {
            font-family: 'Courier New', monospace;
            font-weight: bold;
            color: #e53e3e;
            font-size: 13px;
        }
        
        .status-badge {
            padding: 5px 10px;
            border-radius: 15px;
            font-size: 10px;
            font-weight: bold;
            text-transform: uppercase;
            display: inline-block;
        }
        
        .status-completed { background: #d4edda; color: #155724; }
        .status-live_call_agent { background: #fff3cd; color: #856404; }
        .status-live_call_team { background: #d1ecf1; color: #0c5460; }
        .status-ticket_raised { background: #f8d7da; color: #721c24; }
        
        .btn-details {
            background: #e53e3e;
            color: white;
            padding: 6px 14px;
            border: none;
            border-radius: 6px;
            font-size: 12px;
            cursor: pointer;
            transition: all 0.2s;
        }
        
        .btn-details:hover {
            background: #c53030;
            transform: scale(1.05);
        }
        
        .no-calls {
            text-align: center;
            padding: 60px;
            color: #999;
            font-size: 18px;
        }
        
        /* Side Panel */
        .side-panel {
            position: fixed;
            right: -800px;
            top: 0;
            width: 800px;
            height: 100vh;
            background: white;
            box-shadow: -10px 0 40px rgba(0,0,0,0.3);
            transition: right 0.4s ease;
            z-index: 1000;
            overflow-y: auto;
        }
        
        .side-panel.open { right: 0; }
        
        .panel-header {
            background: linear-gradient(135deg, #e53e3e, #c53030);
            color: white;
            padding: 30px;
            position: sticky;
            top: 0;
            z-index: 10;
        }
        
        .close-btn {
            position: absolute;
            top: 25px;
            right: 30px;
            background: rgba(255,255,255,0.2);
            border: none;
            color: white;
            font-size: 28px;
            width: 40px;
            height: 40px;
            border-radius: 50%;
            cursor: pointer;
            transition: all 0.2s;
        }
        
        .close-btn:hover {
            background: rgba(255,255,255,0.3);
            transform: rotate(90deg);
        }
        
        .panel-content { padding: 30px; }
        
        .info-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 20px;
            margin-bottom: 25px;
        }
        
        .info-box {
            background: #f8f9fa;
            padding: 20px;
            border-radius: 10px;
            border-left: 4px solid #e53e3e;
        }
        
        .info-label {
            font-size: 12px;
            font-weight: 600;
            color: #666;
            text-transform: uppercase;
            margin-bottom: 8px;
        }
        
        .info-value {
            font-size: 16px;
            color: #333;
            font-weight: 600;
        }
        
        .section-title {
            font-size: 18px;
            font-weight: 700;
            color: #333;
            margin: 30px 0 15px 0;
            padding-bottom: 10px;
            border-bottom: 2px solid #e53e3e;
        }
        
        .notes-textarea {
            width: 100%;
            min-height: 120px;
            padding: 15px;
            border: 2px solid #dee2e6;
            border-radius: 8px;
            font-family: inherit;
            font-size: 14px;
            resize: vertical;
        }
        
        .btn-save {
            background: linear-gradient(135deg, #48bb78, #38a169);
            color: white;
            padding: 12px 24px;
            border: none;
            border-radius: 8px;
            font-weight: 600;
            cursor: pointer;
            margin-top: 10px;
        }
        
        .transcript-list {
            background: #f8f9fa;
            border-radius: 10px;
            padding: 20px;
            max-height: 400px;
            overflow-y: auto;
        }
        
        .transcript-msg {
            margin-bottom: 15px;
            padding: 12px;
            border-radius: 8px;
        }
        
        .transcript-msg.CUSTOMER {
            background: #e3f2fd;
            border-left: 4px solid #2196F3;
            margin-left: 20px;
        }
        
        .transcript-msg.AI_AGENT {
            background: #e8f5e8;
            border-left: 4px solid #4caf50;
            margin-right: 20px;
        }
        
        .audio-player {
            background: #f8f9fa;
            padding: 20px;
            border-radius: 10px;
            margin: 20px 0;
        }
        
        audio {
            width: 100%;
            margin-top: 10px;
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="header-top">
            <div>
                <h1>Voice Agent Full Dashboard</h1>
                <p>Complete call history and details</p>
            </div>
            <div style="display: flex; align-items: center; gap: 20px;">
                <a href="/" class="live-calls-btn">← Live Calls</a>
                <div class="logo-container">
                    <div class="logo-box">WK</div>
                    <div class="logo-text">
                        <div class="brand-name">WasteKing</div>
                        <div class="tagline">Waste Management Solutions</div>
                    </div>
                </div>
            </div>
        </div>
        
        <div class="tabs">
            <button class="tab active" onclick="filterCalls('all', this)">All Calls</button>
            <button class="tab" onclick="filterCalls('skip-hire', this)">Skip Hire</button>
            <button class="tab" onclick="filterCalls('man-van', this)">Man & Van</button>
            <button class="tab" onclick="filterCalls('trade', this)">Trade</button>
            <button class="tab" onclick="filterCalls('callbacks', this)">Callbacks</button>
            <button class="tab" onclick="filterCalls('ticket', this)">Tickets</button>
        </div>
    </div>
    
    <div class="stats-grid">
        <div class="stat-card">
            <div class="stat-number" id="totalCalls">0</div>
            <div class="stat-label">Total Calls</div>
        </div>
        <div class="stat-card">
            <div class="stat-number" id="todayCalls">0</div>
            <div class="stat-label">Today's Calls</div>
        </div>
        <div class="stat-card">
            <div class="stat-number" id="callbacks">0</div>
            <div class="stat-label">Callbacks</div>
        </div>
        <div class="stat-card">
            <div class="stat-number" id="complaints">0</div>
            <div class="stat-label">Complaints</div>
        </div>
    </div>
    
    <div class="table-container">
        <div class="table-header">
            <div>TIME</div>
            <div>UNIQUE ID</div>
            <div>CUSTOMER</div>
            <div>POSTCODE</div>
            <div>SERVICE</div>
            <div>STATUS</div>
            <div>MSGS</div>
            <div>ACTIONS</div>
        </div>
        <div id="tableBody"></div>
    </div>
    
    <div class="side-panel" id="sidePanel">
        <div class="panel-header">
            <button class="close-btn" onclick="closePanel()">&times;</button>
            <h2>Call Details</h2>
            <p id="panelCallId" style="opacity: 0.9; margin-top: 8px;"></p>
        </div>
        <div class="panel-content">
            <div class="info-grid">
                <div class="info-box">
                    <div class="info-label">Customer Name</div>
                    <div class="info-value" id="detailName">-</div>
                </div>
                <div class="info-box">
                    <div class="info-label">Phone Number</div>
                    <div class="info-value" id="detailPhone">-</div>
                </div>
                <div class="info-box">
                    <div class="info-label">Postcode</div>
                    <div class="info-value" id="detailPostcode">-</div>
                </div>
                <div class="info-box">
                    <div class="info-label">Service</div>
                    <div class="info-value" id="detailService">-</div>
                </div>
                <div class="info-box">
                    <div class="info-label">Call Status</div>
                    <div class="info-value" id="detailStatus">-</div>
                </div>
                <div class="info-box">
                    <div class="info-label">When Needed</div>
                    <div class="info-value" id="detailWhen">-</div>
                </div>
            </div>
            
            <div id="audioSection" class="audio-player" style="display:none;">
                <h3>Call Recording</h3>
                <audio controls id="audioPlayer"></audio>
            </div>
            
            <h3 class="section-title">Team Notes</h3>
            <textarea class="notes-textarea" id="teamNotes" placeholder="Add team notes here..."></textarea>
            <button class="btn-save" onclick="saveNotes()">Save Notes</button>
            
            <h3 class="section-title">Call Transcript</h3>
            <div class="transcript-list" id="transcriptList"></div>
        </div>
    </div>

    <script>
        let allCalls = [];
        let currentFilter = 'all';
        let selectedCallId = null;
        
        function filterCalls(filter, element) {
            document.querySelectorAll('.tab').forEach(tab => tab.classList.remove('active'));
            element.classList.add('active');
            currentFilter = filter;
            renderTable();
        }
        
        async function loadData() {
            try {
                const [callsRes, statsRes] = await Promise.all([
                    fetch('/api/conversations'),
                    fetch('/api/stats')
                ]);
                
                const callsData = await callsRes.json();
                const statsData = await statsRes.json();
                
                allCalls = callsData.calls || [];
                
                document.getElementById('totalCalls').textContent = statsData.total_calls || 0;
                document.getElementById('todayCalls').textContent = statsData.today_calls || 0;
                document.getElementById('callbacks').textContent = statsData.callbacks || 0;
                document.getElementById('complaints').textContent = statsData.complaints || 0;
                
                renderTable();
            } catch (error) {
                console.error('Error:', error);
            }
        }
        
        function renderTable() {
            const tbody = document.getElementById('tableBody');
            
            let filtered = allCalls;
            if (currentFilter === 'skip-hire') filtered = allCalls.filter(c => c.service === 'Skip Hire');
            else if (currentFilter === 'man-van') filtered = allCalls.filter(c => c.service === 'Man & Van');
            else if (currentFilter === 'trade') filtered = allCalls.filter(c => c.trade_customer);
            else if (currentFilter === 'callbacks') filtered = allCalls.filter(c => c.callback_requested);
            else if (currentFilter === 'ticket') filtered = allCalls.filter(c => c.call_status === 'ticket_raised');
            
            if (filtered.length === 0) {
                tbody.innerHTML = '<div class="no-calls">No calls found</div>';
                return;
            }
            
            tbody.innerHTML = '';
            filtered.forEach(call => {
                const row = document.createElement('div');
                row.className = 'table-row';
                row.onclick = () => openPanel(call.id);
                
                row.innerHTML = `
                    <div>${call.time || '--:--'}</div>
                    <div class="call-id-cell">${call.unique_call_id}</div>
                    <div>${call.customer_name || 'Unknown'}</div>
                    <div><strong>${call.postcode || 'Unknown'}</strong></div>
                    <div>${call.service || 'Unknown'}</div>
                    <div><span class="status-badge status-${call.call_status}">${call.call_status.replace('_', ' ').toUpperCase()}</span></div>
                    <div>${call.transcript_count}</div>
                    <div><button class="btn-details" onclick="event.stopPropagation(); openPanel(${call.id})">Details</button></div>
                `;
                tbody.appendChild(row);
            });
        }
        
        async function openPanel(callId) {
            selectedCallId = callId;
            const call = allCalls.find(c => c.id === callId);
            if (!call) return;
            
            document.getElementById('panelCallId').textContent = call.unique_call_id;
            document.getElementById('detailName').textContent = call.customer_name || 'Not provided';
            document.getElementById('detailPhone').textContent = call.from || 'Unknown';
            document.getElementById('detailPostcode').textContent = call.postcode || 'Not provided';
            document.getElementById('detailService').textContent = call.service || 'Unknown';
            document.getElementById('detailStatus').textContent = call.call_status.replace('_', ' ').toUpperCase();
            document.getElementById('detailWhen').textContent = call.when_needed || 'Not specified';
            document.getElementById('teamNotes').value = call.team_notes || '';
            
            // Audio
            const audioSection = document.getElementById('audioSection');
            const audioPlayer = document.getElementById('audioPlayer');
            if (call.has_recording) {
                audioSection.style.display = 'block';
                audioPlayer.src = call.recording_url;
            } else {
                audioSection.style.display = 'none';
            }
            
            // Transcripts
            try {
                const response = await fetch(`/api/conversations/${call.call_sid}`);
                const data = await response.json();
                
                const transcriptList = document.getElementById('transcriptList');
                transcriptList.innerHTML = '';
                
                if (data.transcripts && data.transcripts.length > 0) {
                    data.transcripts.forEach(t => {
                        const div = document.createElement('div');
                        div.className = `transcript-msg ${t.speaker}`;
                        div.innerHTML = `
                            <strong>${t.speaker}:</strong> ${t.text}
                            <div style="font-size: 11px; color: #666; margin-top: 5px;">${t.time}</div>
                        `;
                        transcriptList.appendChild(div);
                    });
                } else {
                    transcriptList.innerHTML = '<div style="text-align:center;color:#999;">No transcript available</div>';
                }
            } catch (error) {
                console.error('Transcript error:', error);
            }
            
            document.getElementById('sidePanel').classList.add('open');
        }
        
        function closePanel() {
            document.getElementById('sidePanel').classList.remove('open');
            selectedCallId = null;
        }
        
        async function saveNotes() {
            if (!selectedCallId) return;
            
            const notes = document.getElementById('teamNotes').value;
            
            try {
                const response = await fetch('/api/update-team-notes', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        call_id: selectedCallId,
                        team_notes: notes
                    })
                });
                
                const data = await response.json();
                if (data.success) {
                    alert('Notes saved successfully');
                } else {
                    alert('Failed to save notes');
                }
            } catch (error) {
                alert('Error saving notes');
            }
        }
        
        loadData();
    </script>
</body>
</html>
'''

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    print("="*60)
    print("WasteKing Voice Agent - Complete System")
    print("="*60)
    print(f"Live Calls Dashboard: http://localhost:{port}/")
    print(f"Full Dashboard: http://localhost:{port}/dashboard")
    print("="*60)
    print("Features:")
    print("✓ Two dashboards only (Live + Full)")
    print("✓ Call recording enabled")
    print("✓ Unique call IDs (WK + timestamp)")
    print("✓ UK timezone (correct time)")
    print("✓ Postcode capture fixed")
    print("✓ Team notes editing")
    print("✓ Status system (completed/live_call_agent/live_call_team/ticket_raised)")
    print("✓ Auto-refresh on Live (3s), manual on Full")
    print("="*60)
    app.run(host='0.0.0.0', port=port, debug=False)
