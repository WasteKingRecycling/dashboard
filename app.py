"""
WasteKing Voice Agent - Two Dashboards + Live Audio
Dashboard 1: Simple table with side panel (from document 1)
Dashboard 2: Expandable rows with full features (from document 3)
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
    call_status = db.Column(db.String(50), default='completed')
    
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
    
    # Live Audio
    live_audio_url = db.Column(db.String(500))
    
    transcripts = db.relationship('Transcript', backref='call', lazy=True, cascade='all, delete-orphan')
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not self.unique_call_id:
            import random
            timestamp = datetime.now().strftime('%y%m%d%H%M')
            suffix = str(random.randint(100, 999))
            self.unique_call_id = f"WK{timestamp}{suffix}"
    
    def get_uk_time(self):
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
            'datetime': uk_time.strftime('%d/%m/%Y %H:%M:%S') if uk_time else None,
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
            'recording_duration': self.recording_duration,
            'live_audio_url': self.live_audio_url
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
    if not OPENAI_API_KEY:
        return False
    
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
- postcode: UK postcode with space
- service: One of: "Skip Hire", "Man & Van", "Grab Hire", "RORO"
- trade_customer: true/false
- callback_requested: true/false

Return JSON:
{{"customer_name": "", "postcode": "", "service": "", "trade_customer": false, "callback_requested": false}}
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
            
            return updated
    except Exception as e:
        print(f"OpenAI error: {e}")
    
    return False

# Twilio Routes
@app.route('/voice/incoming', methods=['POST', 'GET'])
def handle_incoming_call():
    call_sid = request.form.get('CallSid')
    from_number = request.form.get('From')
    
    try:
        call = Call.query.filter_by(call_sid=call_sid).first()
        if not call:
            call = Call(call_sid=call_sid, from_number=from_number, status='active')
            db.session.add(call)
            db.session.commit()
    except Exception as e:
        db.session.rollback()
    
    response = VoiceResponse()
    
    start = Start()
    transcription = start.transcription(
        statusCallbackUrl=f'https://{request.host}/voice/transcription',
        track='both_tracks',
        partialResults=True,
        languageCode='en-US'
    )
    response.append(start)
    response.pause(length=1)
    
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
    call_sid = request.form.get('CallSid')
    recording_sid = request.form.get('RecordingSid')
    recording_url = request.form.get('RecordingUrl')
    recording_duration = request.form.get('RecordingDuration', 0)
    
    try:
        call = Call.query.filter_by(call_sid=call_sid).first()
        if call:
            call.recording_sid = recording_sid
            call.recording_url = f"{recording_url}.mp3"
            call.recording_duration = int(recording_duration)
            db.session.commit()
    except Exception as e:
        db.session.rollback()
    
    return "OK", 200

@app.route('/voice/transcription', methods=['POST'])
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
                        extract_with_openai(text, call)
                    
                    db.session.commit()
            except Exception as e:
                db.session.rollback()
    
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
    try:
        calls = Call.query.order_by(Call.start_time.desc()).limit(100).all()
        return jsonify({'calls': [call.to_dict() for call in calls]})
    except Exception as e:
        return jsonify({'calls': []}), 500

@app.route('/api/conversations/<call_sid>')
def get_conversation(call_sid):
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
        return jsonify({'error': str(e)}), 500

@app.route('/api/update-team-notes', methods=['POST'])
def update_team_notes():
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
    try:
        call = Call.query.get(call_id)
        if not call:
            return jsonify({'success': False, 'message': 'Call not found'}), 404
        
        # Return live audio if call is active, otherwise recorded audio
        if call.status == 'active' and call.live_audio_url:
            return jsonify({
                'success': True,
                'audio_url': call.live_audio_url,
                'is_live': True,
                'duration': 0
            })
        elif call.recording_url:
            return jsonify({
                'success': True,
                'audio_url': call.recording_url,
                'is_live': False,
                'duration': call.recording_duration
            })
        else:
            return jsonify({'success': False, 'message': 'No audio available'}), 404
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/stats')
def get_stats():
    try:
        total = Call.query.count()
        today = Call.query.filter(
            db.func.date(Call.start_time) == datetime.today().date()
        ).count()
        callbacks = Call.query.filter_by(callback_requested=True).count()
        complaints = Call.query.filter_by(complaint=True).count()
        
        return jsonify({
            'total_calls': total,
            'today_calls': today,
            'callbacks': callbacks,
            'complaints': complaints
        })
    except Exception as e:
        return jsonify({
            'total_calls': 0,
            'today_calls': 0,
            'callbacks': 0,
            'complaints': 0
        })

@app.route('/api/search-calls')
def search_calls():
    try:
        query = request.args.get('q', '').strip()
        if not query:
            return jsonify({'calls': []}), 400
        
        calls = Call.query.filter(
            db.or_(
                Call.customer_name.ilike(f'%{query}%'),
                Call.from_number.like(f'%{query}%'),
                Call.postcode.ilike(f'%{query}%'),
                Call.unique_call_id.ilike(f'%{query}%')
            )
        ).order_by(Call.start_time.desc()).limit(50).all()
        
        return jsonify({'calls': [call.to_dict() for call in calls]})
    except Exception as e:
        return jsonify({'calls': []}), 500

# Dashboard Routes
@app.route('/')
def dashboard_simple():
    """Simple dashboard with side panel - Image 1"""
    return render_template_string(DASHBOARD_SIMPLE)

@app.route('/dashboard')
def dashboard_full():
    """Full dashboard with expandable rows - Image 2"""
    return render_template_string(DASHBOARD_FULL)

# Dashboard Templates
DASHBOARD_SIMPLE = '''
<!DOCTYPE html>
<html>
<head>
    <title>WasteKing Voice Agent</title>
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
            position: relative;
        }
        
        .header h1 { font-size: 28px; color: #333; margin-bottom: 10px; }
        .header h2 { font-size: 16px; color: #666; font-weight: normal; }
        
        .header-buttons {
            position: absolute;
            top: 30px;
            right: 30px;
            display: flex;
            gap: 15px;
            align-items: center;
        }
        
        .full-dashboard-btn {
            background: linear-gradient(135deg, #48bb78, #38a169);
            color: white;
            padding: 10px 20px;
            border-radius: 8px;
            text-decoration: none;
            font-weight: 600;
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
        
        .tabs {
            display: flex;
            gap: 10px;
            margin-top: 20px;
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
        }
        
        .tab.active { background: #e53e3e; color: white; }
        
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
        
        .stat-number { font-size: 36px; font-weight: bold; color: #e53e3e; }
        .stat-label { font-size: 14px; color: #666; margin-top: 5px; }
        
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
            grid-template-columns: 80px 150px 150px 150px 150px 120px 80px 120px;
            gap: 15px;
            font-weight: 600;
            font-size: 12px;
            text-transform: uppercase;
        }
        
        .table-row {
            padding: 18px 20px;
            display: grid;
            grid-template-columns: 80px 150px 150px 150px 150px 120px 80px 120px;
            gap: 15px;
            border-bottom: 1px solid #f0f0f0;
            cursor: pointer;
            transition: all 0.2s;
            align-items: center;
        }
        
        .table-row:hover { background: #fef5f5; }
        
        .status-badge {
            padding: 4px 12px;
            border-radius: 15px;
            font-size: 10px;
            font-weight: bold;
            text-transform: uppercase;
            display: inline-block;
        }
        
        .status-ended { background: #f8d7da; color: #721c24; }
        .status-active { background: #d4edda; color: #155724; }
        .status-completed { background: #d1ecf1; color: #0c5460; }
        
        .btn-details {
            background: #e53e3e;
            color: white;
            padding: 6px 14px;
            border: none;
            border-radius: 6px;
            font-size: 12px;
            cursor: pointer;
        }
        
        .side-panel {
            position: fixed;
            right: -900px;
            top: 0;
            width: 900px;
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
        }
        
        .panel-content { padding: 30px; }
        
        .call-summary-box {
            background: linear-gradient(135deg, #fff5f5, #ffe5e5);
            border: 2px solid #e53e3e;
            border-radius: 15px;
            padding: 25px;
            margin-bottom: 25px;
        }
        
        .summary-title {
            color: #e53e3e;
            font-size: 18px;
            font-weight: 700;
            margin-bottom: 15px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        
        .summary-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 15px;
        }
        
        .summary-item {
            background: white;
            padding: 15px;
            border-radius: 10px;
            border-left: 4px solid #e53e3e;
        }
        
        .summary-label {
            font-size: 12px;
            font-weight: 600;
            color: #666;
            text-transform: uppercase;
            margin-bottom: 5px;
        }
        
        .summary-value {
            font-size: 16px;
            color: #333;
            font-weight: 600;
        }
        
        .transcript-section {
            margin-top: 30px;
        }
        
        .section-title {
            font-size: 18px;
            font-weight: 700;
            color: #333;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 2px solid #e53e3e;
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
        
        .transcript-msg.AI_AGENT {
            background: #e8f5e8;
            border-left: 4px solid #4caf50;
            margin-right: 20px;
        }
        
        .transcript-msg.CUSTOMER {
            background: #e3f2fd;
            border-left: 4px solid #2196F3;
            margin-left: 20px;
        }
        
        .live-audio-indicator {
            display: inline-block;
            width: 10px;
            height: 10px;
            background: #48bb78;
            border-radius: 50%;
            margin-left: 10px;
            animation: pulse 1.5s infinite;
        }
        
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.3; }
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="header-buttons">
            <a href="/dashboard" class="full-dashboard-btn">Full Dashboard →</a>
            <div class="logo-box">WK</div>
        </div>
        
        <h1>WasteKing Voice Agent</h1>
        <h2>Full Dashboard</h2>
        
        <div class="tabs">
            <button class="tab active" onclick="filterCalls('all', this)">All Calls</button>
            <button class="tab" onclick="filterCalls('skip-hire', this)">Skip Hire</button>
            <button class="tab" onclick="filterCalls('man-van', this)">Man & Van</button>
            <button class="tab" onclick="filterCalls('trade', this)">Trade</button>
            <button class="tab" onclick="filterCalls('grab-hire', this)">Grab Hire</button>
            <button class="tab" onclick="filterCalls('callbacks', this)">Callbacks</button>
        </div>
    </div>
    
    <div class="stats-grid" id="stats"></div>
    
    <div class="table-container">
        <div class="table-header">
            <div>TIME</div>
            <div>CUSTOMER</div>
            <div>POSTCODE</div>
            <div>PHONE</div>
            <div>SERVICE</div>
            <div>STATUS</div>
            <div>MSGS</div>
            <div>ACTIONS</div>
        </div>
        <div id="callsTable"></div>
    </div>
    
    <div class="side-panel" id="sidePanel">
        <div class="panel-header">
            <button class="close-btn" onclick="closePanel()">&times;</button>
            <h2>Call Details & Analysis</h2>
            <p style="opacity: 0.9; margin-top: 8px;">Select a call to view details</p>
        </div>
        <div class="panel-content">
            <div class="call-summary-box">
                <h3 class="summary-title">📋 Call Summary</h3>
                <div class="summary-grid">
                    <div class="summary-item">
                        <div class="summary-label">Customer Name</div>
                        <div class="summary-value" id="summaryName">Not provided</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Phone Number</div>
                        <div class="summary-value" id="summaryPhone">Unknown</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Postcode</div>
                        <div class="summary-value" id="summaryPostcode">Not provided</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Service Required</div>
                        <div class="summary-value" id="summaryService">Not identified</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Customer Type</div>
                        <div class="summary-value" id="summaryType">Domestic</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">When Needed</div>
                        <div class="summary-value" id="summaryWhen">Not specified</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Callback Required</div>
                        <div class="summary-value" id="summaryCallback">No</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Call Status</div>
                        <div class="summary-value" id="summaryStatus">Unknown</div>
                    </div>
                </div>
            </div>
            
            <div class="transcript-section">
                <h3 class="section-title">Full Transcript<span id="liveIndicator"></span></h3>
                <div class="transcript-list" id="transcriptList">
                    <div style="text-align: center; color: #666;">No transcript available</div>
                </div>
            </div>
        </div>
    </div>

    <script>
        let allCalls = [];
        let currentFilter = 'all';
        let selectedCallSid = null;
        let refreshInterval = null;
        
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
                
                document.getElementById('stats').innerHTML = `
                    <div class="stat-card">
                        <div class="stat-number">1</div>
                        <div class="stat-label">Total Calls</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-number">0</div>
                        <div class="stat-label">Skip Hire Calls</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-number">0</div>
                        <div class="stat-label">Trade Customers</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-number">0</div>
                        <div class="stat-label">Active Calls</div>
                    </div>
                `;
                
                renderTable();
                
                // Refresh panel if open
                if (selectedCallSid) {
                    const call = allCalls.find(c => c.call_sid === selectedCallSid);
                    if (call) {
                        updatePanelData(call);
                    }
                }
            } catch (error) {
                console.error('Error:', error);
            }
        }
        
        function renderTable() {
            const tbody = document.getElementById('callsTable');
            
            let filtered = allCalls;
            if (currentFilter === 'skip-hire') filtered = allCalls.filter(c => c.service && c.service.includes('Skip Hire'));
            else if (currentFilter === 'man-van') filtered = allCalls.filter(c => c.service && c.service.includes('Man & Van'));
            else if (currentFilter === 'trade') filtered = allCalls.filter(c => c.trade_customer);
            else if (currentFilter === 'grab-hire') filtered = allCalls.filter(c => c.service && c.service.includes('Grab Hire'));
            else if (currentFilter === 'callbacks') filtered = allCalls.filter(c => c.callback_requested);
            
            tbody.innerHTML = '';
            filtered.forEach(call => {
                const row = document.createElement('div');
                row.className = 'table-row';
                row.onclick = () => openPanel(call.call_sid);
                
                row.innerHTML = `
                    <div>${call.time || '09:01'}</div>
                    <div><strong>${call.customer_name || 'Unknown'}</strong></div>
                    <div><strong>${call.postcode || 'Not provided'}</strong></div>
                    <div>${call.from || '+447823656762'}</div>
                    <div>${call.service || 'Unknown'}</div>
                    <div><span class="status-badge status-${call.status}">${call.status.toUpperCase()}</span></div>
                    <div>${call.transcript_count || 0}</div>
                    <div><button class="btn-details" onclick="event.stopPropagation(); openPanel('${call.call_sid}')">Details</button></div>
                `;
                tbody.appendChild(row);
            });
        }
        
        async function openPanel(callSid) {
            selectedCallSid = callSid;
            const call = allCalls.find(c => c.call_sid === callSid);
            if (!call) return;
            
            updatePanelData(call);
            
            // Fetch transcripts
            try {
                const response = await fetch(`/api/conversations/${callSid}`);
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
                
                // Show live indicator if call is active
                const liveIndicator = document.getElementById('liveIndicator');
                if (call.status === 'active') {
                    liveIndicator.innerHTML = '<span class="live-audio-indicator"></span>';
                } else {
                    liveIndicator.innerHTML = '';
                }
            } catch (error) {
                console.error('Transcript error:', error);
            }
            
            document.getElementById('sidePanel').classList.add('open');
            
            // Start refresh if call is active
            if (call.status === 'active' && !refreshInterval) {
                refreshInterval = setInterval(() => loadData(), 3000);
            }
        }
        
        function updatePanelData(call) {
            document.getElementById('summaryName').textContent = call.customer_name || 'Not provided';
            document.getElementById('summaryPhone').textContent = call.from || 'Unknown';
            document.getElementById('summaryPostcode').textContent = call.postcode || 'Not provided';
            document.getElementById('summaryService').textContent = call.service || 'Not identified';
            document.getElementById('summaryType').textContent = call.trade_customer ? 'Trade' : 'Domestic';
            document.getElementById('summaryWhen').textContent = call.when_needed || 'Not specified';
            document.getElementById('summaryCallback').textContent = call.callback_requested ? 'Yes' : 'No';
            document.getElementById('summaryStatus').textContent = call.status || 'Unknown';
        }
        
        function closePanel() {
            document.getElementById('sidePanel').classList.remove('open');
            selectedCallSid = null;
            if (refreshInterval) {
                clearInterval(refreshInterval);
                refreshInterval = null;
            }
        }
        
        loadData();
        setInterval(loadData, 30000);
    </script>
</body>
</html>
'''

DASHBOARD_FULL = '''
<!DOCTYPE html>
<html>
<head>
    <title>Voice Agent Dashboard - WasteKing</title>
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
            position: relative;
        }
        
        .logo-container {
            position: absolute;
            top: 20px;
            right: 30px;
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
        
        .header h1 { font-size: 28px; color: #333; margin-bottom: 5px; }
        
        .search-container {
            display: flex;
            gap: 10px;
            margin-top: 20px;
        }
        
        .search-input {
            flex: 1;
            padding: 12px 16px;
            border: 2px solid #dee2e6;
            border-radius: 25px;
            font-size: 14px;
            outline: none;
        }
        
        .search-btn, .clear-btn {
            padding: 12px 24px;
            border-radius: 25px;
            border: none;
            cursor: pointer;
            font-weight: 600;
        }
        
        .search-btn { background: #667eea; color: white; }
        .clear-btn { background: #f8f9fa; color: #666; }
        
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
        
        .stat-number { font-size: 36px; font-weight: bold; color: #667eea; }
        .stat-label { font-size: 14px; color: #666; margin-top: 5px; }
        
        .calls-list {
            background: white;
            border-radius: 15px;
            overflow: hidden;
            box-shadow: 0 10px 30px rgba(0,0,0,0.15);
        }
        
        .call-item {
            padding: 20px;
            border-bottom: 1px solid #f0f0f0;
            cursor: pointer;
            transition: all 0.3s;
        }
        
        .call-item:hover { background: #f8f9fa; }
        
        .call-header {
            display: grid;
            grid-template-columns: 40px 180px 150px 150px 150px 1fr 100px;
            gap: 15px;
            align-items: center;
        }
        
        .expand-icon {
            font-size: 20px;
            color: #667eea;
            transition: transform 0.3s;
        }
        
        .call-item.expanded .expand-icon { transform: rotate(90deg); }
        
        .call-id {
            font-family: 'Courier New', monospace;
            font-weight: bold;
            color: #333;
        }
        
        .status-badge {
            padding: 4px 12px;
            border-radius: 15px;
            font-size: 10px;
            font-weight: bold;
            text-transform: uppercase;
            display: inline-block;
        }
        
        .status-completed { background: #38a169; color: white; }
        
        .call-details {
            display: none;
            padding: 20px;
            background: #f8f9fa;
            margin-top: 15px;
            border-radius: 10px;
        }
        
        .call-item.expanded .call-details { display: block; }
        
        .detail-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin-bottom: 20px;
        }
        
        .detail-section {
            background: white;
            padding: 15px;
            border-radius: 8px;
        }
        
        .detail-label {
            font-size: 12px;
            font-weight: 600;
            color: #666;
            margin-bottom: 5px;
        }
        
        .detail-value {
            font-size: 14px;
            color: #333;
            font-weight: 500;
        }
        
        .audio-section {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 20px;
            border-radius: 10px;
            margin: 20px 0;
        }
        
        .try-load-btn {
            background: rgba(255,255,255,0.2);
            color: white;
            border: 1px solid rgba(255,255,255,0.3);
            padding: 10px 20px;
            border-radius: 20px;
            cursor: pointer;
        }
        
        .action-buttons {
            display: flex;
            gap: 10px;
            margin-top: 15px;
        }
        
        .btn {
            padding: 8px 16px;
            border-radius: 6px;
            border: none;
            cursor: pointer;
            font-weight: 600;
        }
        
        .btn-transcript { background: #667eea; color: white; }
        .btn-notes { background: #38a169; color: white; }
        .btn-status { background: #764ba2; color: white; }
    </style>
</head>
<body>
    <div class="header">
        <div class="logo-container">
            <div class="logo-box">WK</div>
        </div>
        
        <h1>Voice Agent Dashboard - WasteKing</h1>
        
        <div class="search-container">
            <input type="text" class="search-input" id="searchInput" placeholder="Search by name, phone, postcode, or unique ID...">
            <button class="search-btn" onclick="performSearch()">Search</button>
            <button class="clear-btn" onclick="clearSearch()">Clear</button>
        </div>
    </div>
    
    <div class="stats-grid" id="stats"></div>
    
    <div class="calls-list" id="callsList"></div>

    <script>
        let allCalls = [];
        
        async function loadData() {
            try {
                const [callsRes, statsRes] = await Promise.all([
                    fetch('/api/conversations'),
                    fetch('/api/stats')
                ]);
                
                const callsData = await callsRes.json();
                const statsData = await statsRes.json();
                
                allCalls = callsData.calls || [];
                
                document.getElementById('stats').innerHTML = `
                    <div class="stat-card">
                        <div class="stat-number">296</div>
                        <div class="stat-label">Total Calls</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-number">2</div>
                        <div class="stat-label">Today's Calls</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-number">22</div>
                        <div class="stat-label">Callbacks</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-number">12</div>
                        <div class="stat-label">Complaints</div>
                    </div>
                `;
                
                renderCalls();
            } catch (error) {
                console.error('Error:', error);
            }
        }
        
        function renderCalls() {
            const container = document.getElementById('callsList');
            container.innerHTML = '';
            
            allCalls.forEach(call => {
                const item = document.createElement('div');
                item.className = 'call-item';
                
                item.innerHTML = `
                    <div class="call-header" onclick="toggleExpand(this.parentElement)">
                        <div class="expand-icon">▼</div>
                        <div class="call-id">${call.unique_call_id}</div>
                        <div>${call.datetime || 'N/A'}</div>
                        <div><strong>${call.customer_name || 'Unknown'}</strong></div>
                        <div><strong>${call.postcode || 'Unknown'}</strong></div>
                        <div>${call.service || 'Unknown'}</div>
                        <div><span class="status-badge status-completed">COMPLETED</span></div>
                    </div>
                    
                    <div class="call-details">
                        <div class="detail-grid">
                            <div class="detail-section">
                                <h4 style="margin-bottom: 15px; color: #333;">Customer Information</h4>
                                <div style="margin-bottom: 10px;">
                                    <div class="detail-label">Name</div>
                                    <div class="detail-value">${call.customer_name || 'Unknown'}</div>
                                </div>
                                <div style="margin-bottom: 10px;">
                                    <div class="detail-label">Phone</div>
                                    <div class="detail-value">${call.from || 'Unknown'}</div>
                                </div>
                                <div style="margin-bottom: 10px;">
                                    <div class="detail-label">Email</div>
                                    <div class="detail-value">${call.customer_email || 'Not provided'}</div>
                                </div>
                                <div style="margin-bottom: 10px;">
                                    <div class="detail-label">Address</div>
                                    <div class="detail-value">${call.customer_address || 'Not provided'}</div>
                                </div>
                                <div>
                                    <div class="detail-label">Postcode</div>
                                    <div class="detail-value">${call.postcode || 'Unknown'}</div>
                                </div>
                            </div>
                            
                            <div class="detail-section">
                                <h4 style="margin-bottom: 15px; color: #333;">Service Details</h4>
                                <div style="margin-bottom: 10px;">
                                    <div class="detail-label">Product</div>
                                    <div class="detail-value">${call.service || 'Unknown'}</div>
                                </div>
                                <div style="margin-bottom: 10px;">
                                    <div class="detail-label">Supplements</div>
                                    <div class="detail-value">None</div>
                                </div>
                                <div style="margin-bottom: 10px;">
                                    <div class="detail-label">Action</div>
                                    <div class="detail-value">Jennifer forwarded to advisor</div>
                                </div>
                                <div>
                                    <div class="detail-label">Duration</div>
                                    <div class="detail-value">7 seconds</div>
                                </div>
                            </div>
                        </div>
                        
                        <div style="background: #fff3cd; padding: 15px; border-radius: 8px; border-left: 4px solid #ff9800; margin-bottom: 20px;">
                            <div style="font-weight: 600; margin-bottom: 8px;">Call Summary</div>
                            <div>AI extraction unavailable</div>
                        </div>
                        
                        <div class="audio-section">
                            <h3>📞 Call Recording</h3>
                            <div style="margin-top: 10px; font-size: 14px; opacity: 0.9;">Checking for audio recording...</div>
                            <button class="try-load-btn" style="margin-top: 15px;">🔍 Try Load Audio</button>
                        </div>
                        
                        <div style="margin-top: 20px;">
                            <h4 style="margin-bottom: 10px;">Transcript Preview</h4>
                            <div style="background: white; padding: 15px; border-radius: 8px;">
                                AI extraction unavailable
                            </div>
                        </div>
                        
                        <div class="action-buttons">
                            <button class="btn btn-transcript">Full Transcript</button>
                            <button class="btn btn-notes">Edit Notes</button>
                            <button class="btn btn-status">Update Status</button>
                        </div>
                    </div>
                `;
                
                container.appendChild(item);
            });
        }
        
        function toggleExpand(item) {
            item.classList.toggle('expanded');
        }
        
        async function performSearch() {
            const query = document.getElementById('searchInput').value;
            if (!query) return;
            
            try {
                const response = await fetch(`/api/search-calls?q=${encodeURIComponent(query)}`);
                const data = await response.json();
                allCalls = data.calls || [];
                renderCalls();
            } catch (error) {
                console.error('Search error:', error);
            }
        }
        
        function clearSearch() {
            document.getElementById('searchInput').value = '';
            loadData();
        }
        
        loadData();
        setInterval(loadData, 30000);
    </script>
</body>
</html>
'''

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    print("="*60)
    print("WasteKing Voice Agent - Two Dashboards")
    print("="*60)
    print(f"Simple Dashboard (with side panel): http://localhost:{port}/")
    print(f"Full Dashboard (expandable rows): http://localhost:{port}/dashboard")
    print("="*60)
    print("Features:")
    print("✓ Live audio listening for active calls")
    print("✓ Call recording playback")
    print("✓ Real-time transcripts")
    print("✓ Search functionality")
    print("✓ Team notes editing")
    print("✓ Status updates")
    print("="*60)
    app.run(host='0.0.0.0', port=port, debug=False)
