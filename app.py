"""
WasteKing Voice Agent - Complete System with Both Dashboards
Live Calls Dashboard + Full Dashboard with Call Recording
"""

from flask import Flask, request, jsonify, render_template_string, send_file
from flask_sqlalchemy import SQLAlchemy
from twilio.twiml.voice_response import VoiceResponse, Start
from twilio.rest import Client
import os
import json
import requests
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
    
    # Status
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
- postcode: UK postcode with space (e.g., "LS14 8AB")
- service: One of: "Skip Hire", "Man & Van", "Grab Hire", "RORO", "Toilet Hire"
- trade_customer: true/false
- callback_requested: true/false
- when_needed: When service needed

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
        print(f"Database error: {e}")
    
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
    
    from twilio.twiml.voice_response import Dial
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
        print(f"Recording error: {e}")
    
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
        two_hours_ago = datetime.utcnow() - timedelta(hours=2)
        calls = Call.query.filter(Call.start_time >= two_hours_ago).order_by(Call.start_time.desc()).all()
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

@app.route('/api/stats')
def get_stats():
    try:
        total = Call.query.count()
        today = Call.query.filter(
            db.func.date(Call.start_time) == datetime.today().date()
        ).count()
        callbacks = Call.query.filter_by(callback_requested=True).count()
        complaints = Call.query.filter_by(complaint=True).count()
        active = Call.query.filter_by(status='active').count()
        skip_hire = Call.query.filter(Call.service.like('%Skip Hire%')).count()
        trade = Call.query.filter_by(trade_customer=True).count()
        
        return jsonify({
            'total_calls': total,
            'today_calls': today,
            'callbacks': callbacks,
            'complaints': complaints,
            'active_calls': active,
            'skip_hire_calls': skip_hire,
            'trade_customers': trade
        })
    except Exception as e:
        return jsonify({
            'total_calls': 0,
            'today_calls': 0,
            'callbacks': 0,
            'complaints': 0,
            'active_calls': 0,
            'skip_hire_calls': 0,
            'trade_customers': 0
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
def live_dashboard():
    """Live Calls Dashboard - Simple Clean Interface"""
    return render_template_string(LIVE_CALLS_DASHBOARD)

@app.route('/dashboard')
def full_dashboard():
    """Full Dashboard - Detailed Call Records"""
    return render_template_string(FULL_DASHBOARD)

# HTML Templates
LIVE_CALLS_DASHBOARD = '''
<!DOCTYPE html>
<html>
<head>
    <title>WasteKing Voice Agent</title>
    <meta http-equiv="refresh" content="5">
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
        
        .live-btn {
            background: #38a169;
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
            align-items: center;
        }
        
        .status-badge {
            padding: 4px 12px;
            border-radius: 15px;
            font-size: 10px;
            font-weight: bold;
            text-transform: uppercase;
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
    </style>
</head>
<body>
    <div class="header" style="position: relative;">
        <div class="header-buttons">
            <a href="/dashboard" class="live-btn">Full Dashboard →</a>
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

    <script>
        let allCalls = [];
        let currentFilter = 'all';
        
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
                        <div class="stat-number">${statsData.total_calls}</div>
                        <div class="stat-label">Total Calls</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-number">${statsData.skip_hire_calls}</div>
                        <div class="stat-label">Skip Hire Calls</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-number">${statsData.trade_customers}</div>
                        <div class="stat-label">Trade Customers</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-number">${statsData.active_calls}</div>
                        <div class="stat-label">Active Calls</div>
                    </div>
                `;
                
                renderTable();
            } catch (error) {
                console.error('Error:', error);
            }
        }
        
        function filterCalls(filter, element) {
            document.querySelectorAll('.tab').forEach(tab => tab.classList.remove('active'));
            element.classList.add('active');
            currentFilter = filter;
            renderTable();
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
                
                row.innerHTML = `
                    <div>${call.time || '--:--'}</div>
                    <div>${call.customer_name || 'Unknown'}</div>
                    <div>${call.postcode || 'Not provided'}</div>
                    <div>${call.from || 'Unknown'}</div>
                    <div>${call.service || 'Unknown'}</div>
                    <div><span class="status-badge status-${call.status}">${call.status.toUpperCase()}</span></div>
                    <div>${call.transcript_count}</div>
                    <div><button class="btn-details">Details</button></div>
                `;
                tbody.appendChild(row);
            });
        }
        
        loadData();
    </script>
</body>
</html>
'''

FULL_DASHBOARD = '''
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
        
        .logo-text {
            display: flex;
            flex-direction: column;
        }
        
        .brand-name { font-size: 20px; font-weight: 700; color: #333; }
        .tagline { font-size: 11px; color: #666; }
        
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
            transition: all 0.3s;
        }
        
        .search-btn { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; }
        .clear-btn { background: #f8f9fa; color: #666; border: 2px solid #dee2e6; }
        
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
            display: grid;
            grid-template-columns: 40px 150px 150px 150px 200px 1fr 100px;
            gap: 15px;
            align-items: center;
        }
        
        .call-item:hover { background: #f8f9fa; }
        
        .expand-icon { font-size: 20px; color: #667eea; transition: transform 0.3s; }
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
            border-top: 1px solid #dee2e6;
        }
        
        .call-item.expanded .call-details { display: block; }
        
        .detail-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
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
            text-transform: uppercase;
            margin-bottom: 5px;
        }
        
        .detail-value {
            font-size: 14px;
            color: #333;
            font-weight: 500;
        }
        
        .phone-btn {
            background: #17a2b8;
            color: white;
            border: none;
            padding: 6px 12px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 12px;
            margin-top: 5px;
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="logo-container">
            <div class="logo-box">WK</div>
            <div class="logo-text">
                <div class="brand-name">WasteKing</div>
                <div class="tagline">Professional Waste Management</div>
            </div>
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
                        <div class="stat-number">${statsData.total_calls}</div>
                        <div class="stat-label">Total Calls</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-number">${statsData.today_calls}</div>
                        <div class="stat-label">Today's Calls</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-number">${statsData.callbacks}</div>
                        <div class="stat-label">Callbacks</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-number">${statsData.complaints}</div>
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
                item.onclick = () => toggleExpand(item);
                
                item.innerHTML = `
                    <div class="expand-icon">▶</div>
                    <div class="call-id">${call.unique_call_id}</div>
                    <div class="datetime">${call.datetime || 'N/A'}</div>
                    <div><strong>${call.customer_name || 'Unknown'}</strong></div>
                    <div><strong>${call.postcode || 'Unknown'}</strong></div>
                    <div>${call.service || 'Unknown'}</div>
                    <div><span class="status-badge status-${call.call_status}">${call.call_status.toUpperCase()}</span></div>
                    
                    <div class="call-details" onclick="event.stopPropagation()">
                        <div class="detail-grid">
                            <div class="detail-section">
                                <div class="detail-label">Customer Name</div>
                                <div class="detail-value">${call.customer_name || 'Unknown'}</div>
                            </div>
                            <div class="detail-section">
                                <div class="detail-label">Phone Number</div>
                                <div class="detail-value">
                                    ${call.from || 'Unknown'}
                                    ${call.from && call.from !== 'Unknown' ? `<br><button class="phone-btn" onclick="makeCall('${call.from}', '${call.customer_name}')">📞 Call</button>` : ''}
                                </div>
                            </div>
                            <div class="detail-section">
                                <div class="detail-label">Postcode</div>
                                <div class="detail-value">${call.postcode || 'Not provided'}</div>
                            </div>
                            <div class="detail-section">
                                <div class="detail-label">Service</div>
                                <div class="detail-value">${call.service || 'Unknown'}</div>
                            </div>
                            <div class="detail-section">
                                <div class="detail-label">When Needed</div>
                                <div class="detail-value">${call.when_needed || 'Not specified'}</div>
                            </div>
                            <div class="detail-section">
                                <div class="detail-label">Messages</div>
                                <div class="detail-value">${call.transcript_count}</div>
                            </div>
                        </div>
                    </div>
                `;
                
                container.appendChild(item);
            });
        }
        
        function toggleExpand(item) {
            item.classList.toggle('expanded');
        }
        
        function makeCall(phone, name) {
            alert(`Calling ${name} at ${phone}`);
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
    print("WasteKing Voice Agent - Complete System")
    print("="*60)
    print(f"Live Calls Dashboard: http://localhost:{port}/")
    print(f"Full Dashboard: http://localhost:{port}/dashboard")
    print("="*60)
    app.run(host='0.0.0.0', port=port, debug=False)
