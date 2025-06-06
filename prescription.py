from flask import Flask, request, jsonify, render_template
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import os
import requests
import json
from dotenv import load_dotenv
load_dotenv()


app = Flask(__name__)
scheduler = BackgroundScheduler()
scheduler.start()

# First set the variable in your environment before running:
# set MAILJET_API_KEY=your_public_key
# set MAILJET_SECRET_KEY=your_secret_key
MAILJET_API_KEY = os.getenv('MAILJET_API_KEY')
MAILJET_SECRET_KEY = os.getenv('MAILJET_API_SECRET')
print("📧 API Key:", MAILJET_API_KEY)
print("📧 API Secret:", MAILJET_SECRET_KEY)


# Configure with your Mailjet API key
FROM_EMAIL = 'anshul.saini1507@gmail.com'  # Make sure this email is verified with Mailjet
FROM_NAME = 'Medical Reminder'  # Changed from "Prescription System" to avoid spam triggers

# Email subject templates that are less likely to trigger spam filters
SUBJECT_TEMPLATES = [
    "Your scheduled health reminder - {medicine}",
    "Health reminder: Time for {medicine}",
    "Daily health reminder for {medicine}",
    "Important: Your medication reminder",
    "Medical reminder for {medicine} - {time}"
]

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

@app.route('/')
def index():
    return render_template('prescription.html')

@app.route('/test')
def test_api_page():
    """Serve the API testing page"""
    return render_template('test_api.html')

@app.route('/api/schedule', methods=['POST'])
def schedule_reminders():
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

# Add a new route for whitelist instructions
@app.route('/whitelist-instructions')
def whitelist_instructions():
    return render_template('whitelist_instructions.html')

if __name__ == '__main__':
    app.run(debug=False, use_reloader=False)