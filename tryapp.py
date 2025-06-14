from flask import Flask, render_template, jsonify, request, redirect, url_for, session
import os
from dotenv import load_dotenv
import subprocess
from werkzeug.utils import secure_filename
import tempfile
import uuid
from collections import deque
from datetime import datetime, timedelta
import base64
import requests
from places import MedicalPlacesSystem
from apscheduler.schedulers.background import BackgroundScheduler
import json

from langchain_community.document_loaders import PyPDFLoader
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.chains import create_retrieval_chain
from src.lazy_loader import get_embeddings, get_llm, get_retriever, get_question_answer_chain
from src.prompt import get_system_prompt, customize_response
from src.database import get_user_health, create_user, verify_user, init_db

# Initialize the database
init_db()

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-key-123')

# Load environment variables
load_dotenv()

# Configure upload folder
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'pdf'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Store uploaded document IDs
uploaded_docs = {}

# Initialize MedicalPlacesSystem
print("Initializing MedicalPlacesSystem...")
global medical_system
try:
    medical_system = MedicalPlacesSystem()
    print("MedicalPlacesSystem initialized successfully")
except Exception as e:
    print(f"Error initializing MedicalPlacesSystem: {str(e)}")
    import traceback
    print(f"Full traceback: {traceback.format_exc()}")
    medical_system = None

# Initialize scheduler for prescriptions
scheduler = BackgroundScheduler()
scheduler.start()

# Configure email settings for the prescription system
MAILJET_API_KEY = os.getenv('MAILJET_API_KEY')
MAILJET_SECRET_KEY = os.getenv('MAILJET_API_SECRET')
FROM_EMAIL = 'anshul.saini1507@gmail.com'
FROM_NAME = 'Medical Reminder'

def init_session():
    """Initialize session variables if they don't exist"""
    if 'conversation_history' not in session:
        session['conversation_history'] = []
    if 'current_context' not in session:
        session['current_context'] = {}

def format_conversation_history():
    """Format conversation history for context"""
    if 'conversation_history' not in session:
        return ""
    
    formatted_history = []
    for exchange in session['conversation_history'][-5:]:
        formatted_history.append(f"User: {exchange['user']}")
        formatted_history.append(f"Assistant: {exchange['assistant']}")
    return "\n".join(formatted_history)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def process_pdf(file_path):
    """Process PDF file and return text chunks"""
    loader = PyPDFLoader(file_path)
    pages = loader.load()
    
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=200,
        length_function=len
    )
    
    chunks = text_splitter.split_documents(pages)
    return chunks

@app.route('/get', methods=["POST"])
def get_response():
    msg = request.form["msg"]
    print("User input:", msg)
    
    try:
        # Initialize session if needed
        init_session()
        
        # Get conversation context
        context = format_conversation_history()
        
        # Get user's health information
        health_info = None
        if 'user_id' in session and session['user_id'] != 'guest':
            health_info = get_user_health(session['user_id'])
        
        # Get only the most recent document ID
        recent_doc_id = None
        if 'uploaded_docs' in session and session['uploaded_docs']:
            recent_doc_id = session['uploaded_docs'][-1]
        
        # Configure retriever with document filter
        search_kwargs = {"k": 3}  # Reduced from 5 to 3
        if recent_doc_id:
            search_kwargs["filter"] = {"doc_id": recent_doc_id}
        
        # Get retriever lazily
        retriever = get_retriever(k=3)
        
        # Get question answer chain lazily
        question_answer_chain = get_question_answer_chain()
        
        # Create retrieval chain
        rag_chain = create_retrieval_chain(retriever, question_answer_chain)
        
        # Get relevant documents from Pinecone
        response = rag_chain.invoke({
            "input": msg,
            "context": context
        })
        answer = response["answer"]
    
        # Customize response based on user's health information
        if health_info:
            answer = customize_response(
                answer,
                symptoms=health_info.get('symptoms'),
                diseases=health_info.get('diseases')
            )
        
        print("Response:", answer)
            
        # Update conversation history in session
        session['conversation_history'].append({
            "user": msg,
            "assistant": answer
        })
        
        # Keep only last 5 exchanges
        if len(session['conversation_history']) > 5:
            session['conversation_history'] = session['conversation_history'][-5:]
        
        # Update current context
        session['current_context'] = {
            "last_question": msg,
            "last_answer": answer
        }
        
        # Ensure session changes are saved
        session.modified = True
        
        return str(answer)
    except Exception as e:
        print("Error:", str(e))
        return "I apologize, but I encountered an error while processing your question. Please try again."

@app.route('/')
def index():
    return render_template('index.html')  # Homepage

@app.route('/chat')
def chat():
    print("Current session data:", dict(session))  # Debug log
    
    # Allow both logged-in users and guests
    if 'user_id' not in session:
        print("No user_id in session, redirecting to signin")  # Debug log
        return redirect(url_for('signin'))
    
    # Pre-load the model when entering chat
    try:
        # Initialize model components
        print("Pre-loading model components...")
        get_embeddings()
        get_llm()
        
        print("Model components loaded successfully")
        
        # Set model_loaded flag in session
        session['model_loaded'] = True
        session.modified = True
    except Exception as e:
        print(f"Error pre-loading model: {str(e)}")
        session['model_loaded'] = False
        session.modified = True
    
    return render_template('chat2.html', model_loaded=session.get('model_loaded', False))


@app.route('/chat-with-faq')
def chat_with_faq():
    print("Loading chat with FAQ buttons")  # Debug log
    
    # Allow both logged-in users and guests
    if 'user_id' not in session:
        print("No user_id in session, redirecting to signin")  # Debug log
        return redirect(url_for('signin'))
        
    return render_template('chat2.html')

@app.route('/main')
def main():
    # Redirect to signin if not logged in
    if 'user_id' not in session:
        return redirect(url_for('signin'))
    return render_template('chat2.html')

@app.get("/signin")  # Add this route
def signin():
    return render_template("signin.html")

@app.route('/signup', methods=['POST'])
def signup():
    data = request.get_json()
    success = create_user(
        data['name'],
        data['email'],
        data['password'],
        data.get('symptoms'),
        data.get('diseases')
    )
    
    return jsonify({
        'success': success,
        'message': 'User created successfully' if success else 'User already exists!'
    })

@app.route('/login', methods=['POST'])
def login():
    
    try:
        
        data = request.get_json()
        print("Login attempt for email:", data['email'])  # Debug log
            
        user = verify_user(data['email'], data['password'])
        
        if user:
                # Clear any existing session
            session.clear()
                
                # Set session data
            session['user_id'] = user['id']
            session['user_email'] = user['email']
            session['user_name'] = user['name']
            session['is_guest'] = False
            session.modified = True
                
            print("Login successful - Session data:", dict(session))  # Debug log
                
            return jsonify({
                    'success': True,
                    'redirect': '/chat',
                    'user_name': user['name']
                })
        else:
                print("Login failed - Invalid credentials")  # Debug log
                return jsonify({
                    'success': False,
                    'message': 'Invalid email or password'
                })
    except Exception as e:
        print("Login error:", str(e))  # Debug log
        return jsonify({
            'success': False,
            'message': 'An error occurred during login'
        }), 500

@app.route('/guest_login', methods=['POST'])
def guest_login():
    """Handle guest login by setting a guest session"""
    try:
        # Clear any existing session
        session.clear()
        
        # Set guest session data
        session['user_id'] = 'guest'
        session['user_email'] = 'guest@example.com'
        session['user_name'] = 'Guest'
        session['is_guest'] = True
        session.modified = True
        
        print("Guest login successful - Session data:", dict(session))  # Debug log
        
        return jsonify({
            'success': True,
            'redirect': '/chat'
        })
    except Exception as e:
        print("Guest login error:", str(e))  # Debug log
        return jsonify({
            'success': False,
            'message': str(e)
        }), 500

@app.route("/upload", methods=["POST"])
def upload_file():
    if 'file' not in request.files:
        print("Error: No file part in request")
        return jsonify({'error': 'No file part'}), 400
        
    file = request.files['file']
    if file.filename == '':
        print("Error: No selected file")
        return jsonify({'error': 'No selected file'}), 400

    if not allowed_file(file.filename):
        print(f"Error: Invalid file type for {file.filename}")
        return jsonify({'error': 'Invalid file type. Only PDF files are allowed.'}), 400

    try:
        # Generate unique document ID
        doc_id = str(uuid.uuid4())
        print(f"Processing file: {file.filename} with doc_id: {doc_id}")
        
        # Save file temporarily
        filename = secure_filename(file.filename)
        temp_dir = tempfile.mkdtemp()
        file_path = os.path.join(temp_dir, filename)
        file.save(file_path)
        print(f"File saved temporarily at: {file_path}")

        # Process PDF and get chunks
        print("Processing PDF...")
        chunks = process_pdf(file_path)
        print(f"Generated {len(chunks)} chunks from PDF")
        
        # Add documents to Pinecone with metadata
        print("Adding chunks to Pinecone...")
        for i, chunk in enumerate(chunks):
            chunk.metadata.update({
                'doc_id': doc_id,
                'chunk_index': i,
                'filename': filename,
                'upload_time': datetime.now().isoformat()
            })
        
        # Add to Pinecone in batches
        batch_size = 50
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i:i + batch_size]
            docsearch.add_documents(documents=batch)
            print(f"Added batch {i//batch_size + 1} to Pinecone")
        
        # Store document info
        uploaded_docs[doc_id] = {
            'filename': filename,
            'chunks': len(chunks),
            'upload_time': datetime.now().isoformat()
        }
        
        print(f"Successfully processed and stored {filename}")
        
        # Clean up temporary files
        os.remove(file_path)
        os.rmdir(temp_dir)

        return jsonify({
            'message': 'File processed and stored successfully',
            'chunks': len(chunks),
            'doc_id': doc_id
        }), 200
        
    except Exception as e:
        print(f"Error processing file: {str(e)}")
        # Clean up temporary files if they exist
        try:
            if 'file_path' in locals():
                os.remove(file_path)
            if 'temp_dir' in locals():
                os.rmdir(temp_dir)
        except:
            pass
        return jsonify({'error': str(e)}), 500

@app.route("/delete_document", methods=["POST"])
def delete_document():
    try:
        data = request.get_json()
        doc_id = data.get('doc_id')
        
        if not doc_id:
            return jsonify({'error': 'No document ID provided'}), 400
            
        if doc_id not in uploaded_docs:
            return jsonify({'error': 'Document not found'}), 404
            
        # Delete document from Pinecone
        docsearch.delete(
            filter={
                "doc_id": doc_id
            }
        )
        
        # Remove from tracking
        del uploaded_docs[doc_id]
        
        return jsonify({
            'message': 'Document deleted successfully'
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route("/cleanup_session", methods=["POST"])
def cleanup_session():
    try:
        data = request.get_json()
        doc_ids = data.get('doc_ids', [])
        
        for doc_id in doc_ids:
            if doc_id in uploaded_docs:
                # Delete document from Pinecone
                docsearch.delete(
                    filter={
                        "doc_id": doc_id
                    }
                )
                # Remove from tracking
                del uploaded_docs[doc_id]
        
        return jsonify({
            'message': 'Session cleaned up successfully'
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route("/get_summary", methods=["POST"])
def get_summary():
    try:
        data = request.get_json()
        doc_id = data.get('doc_id')
        
        if not doc_id:
            return jsonify({'error': 'No document ID provided'}), 400
            
        print(f"Generating summary for doc_id: {doc_id}")  # Debug log
        
        # Get document chunks from Pinecone
        results = docsearch.similarity_search(
            query="",
            k=5,
            filter={"doc_id": doc_id}
        )
        
        if not results:
            print(f"No documents found for doc_id: {doc_id}")
            return jsonify({'summary': 'No content found in document'}), 200
            
        print(f"Found {len(results)} chunks for summarization")
        
        # Combine the chunks into a single text
        content = "\n".join([doc.page_content for doc in results])
        
        # Create a comprehensive medical analysis including treatment and precautions
        comprehensive_prompt = ChatPromptTemplate.from_messages([
            ("system", """You are a medical assistant AI analyzing a medical report. Provide a comprehensive analysis in this EXACT format with emoji markers (no substitutions):

🔹 **Medical Summary**:  
<3-4 line summary of key findings in simple, clear language that states what values are high/low/normal and concludes with positive/negative health outcome>

🔹 **Probable Medical Condition(s)**:  
<Specific conditions suggested by the test results>

🔹 **Recommended Treatment Options**:  
<2-3 treatments or procedures with brief explanations>

🔹 **Precautions & Lifestyle Advice**:  
<2-3 practical tips tailored to the findings>

Format exactly as shown with these headings and emoji markers."""),
            ("human", "Analyze this medical document and provide a comprehensive assessment:\n\n{content}")
        ])
        
        # Generate comprehensive analysis
        comprehensive_chain = comprehensive_prompt | llm
        comprehensive_response = comprehensive_chain.invoke({
            "content": content
        })
        
        # Get response content
        comprehensive_content = comprehensive_response.content
        
        print(f"Comprehensive analysis: {comprehensive_content[:100]}...")
        print(type(comprehensive_content))
        
        # For backwards compatibility, generate a brief summary too
        summary_prompt = ChatPromptTemplate.from_messages([
            ("system", """You are a helpful and friendly medical AI assistant. Given a medical report, respond clearly and simply.

            For each report section (like Hemoglobin, WBC, Platelets, etc.), give a **1–2 line explanation** of what the value means, whether it's high, low, or normal, and what it might indicate. Use only actual numbers from the report — **do not add list numbers (1, 2, 3, etc.)**. You may include percentages if they appear in the report.

            At the end, write a short and easy-to-understand **overall summary** combining everything. Be conversational and human, like you're gently explaining to someone with no medical background.

            Keep everything simple, clear, and non-alarming. Avoid medical jargon unless absolutely necessary. use total less than 120 words
            """),
            ("human", "Give the final response in paragraph a :\n\n{content}")
        ])
        
        summary_chain = summary_prompt | llm
        summary_response = summary_chain.invoke({
            "content": comprehensive_content
        })
        
        summary_content = summary_response.content
        print(type(summary_content))
        
        return jsonify({
            'summary': summary_content,
            'comprehensive_analysis': comprehensive_content
            
        }), 200
        
    except Exception as e:
        print(f"Error generating summary: {str(e)}")
        return jsonify({
            'error': 'Failed to generate summary'
        }), 500

@app.route('/speech_to_text', methods=['POST'])
def speech_to_text():
    try:
        audio_data = request.json['audio_data']
        audio_content = base64.b64decode(audio_data)
        audio_base64 = base64.b64encode(audio_content).decode("utf-8")

        url = f"https://speech.googleapis.com/v1/speech:recognize?key={os.environ.get('GOOGLE_API_KEY', 'AIzaSyAcyY1XoBCNg8qcSYk9oDeChC40-PzkevA')}"
        
        headers = {"Content-Type": "application/json"}
        data = {
            "config": {
                "encoding": "WEBM_OPUS",
                "sampleRateHertz": 48000,
                "languageCode": "en-US"
            },
            "audio": {
                "content": audio_base64
            }
        }

        response = requests.post(url, headers=headers, json=data)
        response_data = response.json()

        if "results" in response_data:
            text = response_data['results'][0]['alternatives'][0]['transcript']
            return jsonify({"text": text})
        else:
            return jsonify({"error": response_data.get('error', {}).get('message', 'Error processing audio')})
    except Exception as e:
        print(f"Speech-to-text error: {str(e)}")  # Debug log
        return jsonify({"error": str(e)}), 500

@app.route('/find_medical_help', methods=['POST'])
def find_medical_help():
    try:
        data = request.get_json()
        disease = data.get('disease')
        location = data.get('location')
        
        print(f"Received request for medical help - Disease: {disease}, Location: {location}")
        
        if not disease or not location:
            print("Missing disease or location in request")
            return jsonify({
                'success': False,
                'message': 'Please provide both disease and location'
            }), 400
            
        # Get recommendations from medical system
        print("Calling medical_system.get_recommendations()")
        try:
            if not medical_system:
                raise Exception("Medical recommendation system not initialized")
            result = medical_system.get_recommendations(disease, location)
            print(f"Result from medical_system: {result}")
        except Exception as e:
            print(f"Error in medical_system.get_recommendations(): {str(e)}")
            return jsonify({
                'success': False,
                'message': f'Error getting recommendations: {str(e)}'
            }), 500
        
        if not result['success']:
            print(f"Failed to get recommendations: {result.get('message')}")
            return jsonify(result), 404
            
        # Format the response for chat display
        response = result['response']
        
        print("Successfully formatted response")
        return jsonify({
            'success': True,
            'response': response
        })
        
    except Exception as e:
        print(f"Error in find_medical_help: {str(e)}")
        import traceback
        print(f"Full traceback: {traceback.format_exc()}")
        return jsonify({
            'success': False,
            'message': f'An error occurred while finding medical help: {str(e)}'
        }), 500

@app.route('/get_random_tips', methods=['GET'])
def get_random_tips():
    """Endpoint to get 5 random medical tips from general_help.txt"""
    try:
        # Read tips from general_help.txt with UTF-8 encoding
        with open('general_help.txt', 'r', encoding='utf-8') as file:
            all_tips = file.readlines()
        
        # Clean up tips (remove line numbers and strip whitespace)
        cleaned_tips = []
        for tip in all_tips:
            # Extract the text after the number and period
            if '.' in tip:
                tip_text = tip.split('.', 1)[1].strip()
                cleaned_tips.append(tip_text)
        
        # Select 5 random tips
        import random
        random_tips = random.sample(cleaned_tips, 5)
        
        return jsonify({
            'success': True,
            'tips': random_tips
        })
    
    except Exception as e:
        print(f"Error getting random tips: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

# Optional: Route to start the Flask app as a subprocess
@app.route('/start-app')
def start_app():
    subprocess.Popen(["python", "app.py"], shell=True)
    return "App started", 200

# ---------- PRESCRIPTION ROUTES ----------

def send_mailjet_email(email, medicine):
    """Send medication reminder email using Mailjet API."""
    time_now = datetime.now().strftime("%I:%M %p")  # 12-hour format with AM/PM
    
    # Use a more spam-filter friendly subject line without special characters
    subject = f"Health Reminder: {medicine['name']} - {time_now}"

    html_body = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Health Reminder</title>
    </head>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">
        <div style="border: 1px solid #e0e0e0; border-radius: 5px; padding: 20px; background-color: #f9f9f9;">
            <h2 style="color: #3f51b5; margin-top: 0;">Health Reminder</h2>
            <p>Hello,</p>
            <p>This is your scheduled reminder about your health routine:</p>
            <div style="background-color: #fff; border-left: 4px solid #3f51b5; padding: 15px; margin: 15px 0;">
                <p style="margin: 5px 0;"><strong>Medication:</strong> {medicine['name']}</p>
                <p style="margin: 5px 0;"><strong>Dosage:</strong> {medicine['dosage']}</p>
                <p style="margin: 5px 0;"><strong>Time:</strong> {time_now}</p>
            </div>
            <p>Taking your medication as directed by your healthcare provider is important for your wellbeing.</p>
            <p>Best regards,<br>Your Healthcare Team</p>
            <p style="font-size: 13px; color: #555;">
                <strong>Important:</strong> If you're seeing this email in your spam folder, please mark it as "Not Spam" 
                and add {FROM_EMAIL} to your contacts to ensure you receive future reminders.
            </p>
        </div>
        <p style="font-size: 12px; color: #777; margin-top: 20px;">
            This is an automated reminder from your healthcare application. To unsubscribe or modify your reminder settings, 
            please visit our app or reply to this email with "STOP".
            <br><br>
            <a href="mailto:{FROM_EMAIL}" style="color: #3f51b5; text-decoration: none;">Contact Support</a>
        </p>
    </body>
    </html>
    """

    text_part = f"""
    HEALTH REMINDER
    
    Hello,
    
    This is your scheduled reminder about your health routine:
    
    Medication: {medicine['name']}
    Dosage: {medicine['dosage']}
    Time: {time_now}
    
    Taking your medication as directed by your healthcare provider is important for your wellbeing.
    
    Best regards,
    Your Healthcare Team
    
    Important: If you're seeing this email in your spam folder, please mark it as "Not Spam" 
    and add {FROM_EMAIL} to your contacts to ensure you receive future reminders.
    
    ---
    This is an automated reminder from your healthcare application. To unsubscribe or modify your reminder settings, 
    please visit our app or reply to this email with "STOP".
    """

    url = "https://api.mailjet.com/v3.1/send"
    headers = {"Content-Type": "application/json"}
    payload = {
        "Messages": [
            {
                "From": {
                    "Email": FROM_EMAIL,
                    "Name": FROM_NAME
                },
                "To": [
                    {
                        "Email": email,
                        "Name": "Patient"
                    }
                ],
                "Subject": subject,
                "HTMLPart": html_body,
                "TextPart": text_part,  # Added plain text alternative
                "Headers": {
                    "List-Unsubscribe": f"<mailto:{FROM_EMAIL}?subject=unsubscribe>",
                    "Precedence": "bulk",
                    "X-Auto-Response-Suppress": "OOF, AutoReply"
                },
                "CustomID": f"MedReminder-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                "CustomCampaign": "MedicationReminders"
            }
        ]
    }

    print(f"📤 Sending reminder to {email}...")
    response = requests.post(url, auth=(MAILJET_API_KEY, MAILJET_SECRET_KEY), headers=headers, json=payload)

    try:
        response_data = response.json()
    except json.JSONDecodeError:
        print("❌ Failed to parse JSON response:")
        print(response.text)
        return

    if response.status_code == 200:
        print("✅ Email sent successfully!")
        print("📨 Message ID(s):", [m.get('To')[0].get('MessageUUID') for m in response_data.get('Messages', [])])
    else:
        print(f"❌ Failed to send email. Status Code: {response.status_code}")
        print("🔧 Response:", response_data)

@app.route('/prescription')
def prescription_page():
    """Return the prescription.html template"""
    return render_template('prescription.html')

@app.route('/api/schedule', methods=['POST'])
def schedule_reminders():
    """API endpoint to schedule medication reminders"""
    data = request.get_json()
    email = data['email']
    
    for medicine in data['medicines']:
        start_time = datetime.strptime(medicine['time'], '%H:%M')
        
        # Schedule for each day of treatment
        for day in range(medicine['days']):
            trigger_time = datetime.now().replace(
                hour=start_time.hour,
                minute=start_time.minute
            ) + timedelta(days=day)
            
            # Reschedule if time has passed today
            if trigger_time < datetime.now():
                trigger_time += timedelta(days=1)
            
            scheduler.add_job(
                send_mailjet_email,
                'date',
                run_date=trigger_time,
                args=[email, medicine]
            )
            print(f"⏰ Scheduled email for '{medicine['name']}' at {trigger_time}")
    
    print("🧠 All current jobs in scheduler:")
    for job in scheduler.get_jobs():
        print(job)
    
    return jsonify({'status': 'success', 'message': 'Reminders scheduled successfully!'})

if __name__ == '__main__':
    app.run(debug=False, use_reloader=False)