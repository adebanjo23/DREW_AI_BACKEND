# routes.py
import json
import os
from typing import Dict, Any

import openai
from datetime import datetime
from datetime import datetime, timedelta

import requests
from fastapi import APIRouter, Request, HTTPException, BackgroundTasks, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from googleapiclient.discovery import build
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db, SessionLocal
from models import (
    Lead,
    Appointment,
    Call,
    DrewLeadCommunication,
    UserLeadCommunication,
    UserDrewCommunication,
    User, Integration
)
from google_integration import (
    create_flow,
    save_integration_to_db,
    get_user_credentials,
    refresh_and_save_credentials, send_email_notification
)
from dotenv import load_dotenv

load_dotenv()

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, user_id: str = None):
    from config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(
            status_code=400,
            detail="Missing Google credentials. Please check your .env file."
        )
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing user_id parameter")

    flow = create_flow()
    state_data = {'user_id': user_id, 'prompt': 'consent'}
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent',
        state=json.dumps(state_data)
    )
    return f'<a href="{authorization_url}">Connect with Google Calendar</a>'


@router.get("/oauth/google/callback")
async def google_callback(request: Request):
    try:
        flow = create_flow()
        state_json = request.query_params.get('state', '{}')
        state_data = json.loads(state_json)
        user_id = state_data.get('user_id')
        if not user_id:
            raise HTTPException(status_code=400, detail="No user ID provided")
        code = request.query_params.get('code')
        if not code:
            raise HTTPException(status_code=400, detail="No authorization code received")

        authorization_response = str(request.url)
        flow.fetch_token(authorization_response=authorization_response)
        credentials = flow.credentials

        if not credentials.refresh_token:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "No refresh token received. Please revoke application access and try again."
                }
            )

        service = build('oauth2', 'v2', credentials=credentials)
        try:
            user_info = service.userinfo().get().execute()
            email = user_info.get('email')
            if not email:
                raise HTTPException(status_code=400, detail="Could not get user email")
        except Exception as e:
            print(f"Error getting user info: {str(e)}")
            email = f"user_{datetime.now().timestamp()}"

        # save_integration_to_db should handle its own db session or be updated similarly.
        save_integration_to_db(credentials, email, int(user_id))
        return {"status": "success", "message": "Successfully connected to Google Calendar", "user_id": user_id}
    except Exception as e:
        print(f"Error in callback: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


from openai import OpenAI

client = OpenAI()


def clean_generated_email(email_text: str) -> str:
    """
    Clean up the generated email content:
      - Remove markdown fences if present.
      - Remove a stray leading "html" line if it exists.
    """
    email_text = email_text.strip()

    # Remove markdown fences if present.
    if email_text.startswith("```") and email_text.endswith("```"):
        email_text = email_text[3:-3].strip()

    # Remove a stray leading "html" line if it exists.
    lines = email_text.splitlines()
    if lines and lines[0].strip().lower() == "html":
        email_text = "\n".join(lines[1:]).strip()

    return email_text


def draft_sms_via_ai(user, lead, message_content):
    """
    Uses the OpenAI Chat API to generate a concise and professional SMS message.

    The SMS message:
      - Greets the lead by their first name.
      - Mentions the sender's brokerage using its actual name.
      - Signs off with the sender's first name.
      - Incorporates the provided message content.

    No placeholder text (such as [brokerage_name]) or invented contact details should appear.
    """
    # Extract first names for clarity
    sender_first_name = user.name.split()[0]
    lead_first_name = lead.name.split()[0]

    messages = [
        {
            "role": "system",
            "content": "You are a professional assistant that drafts concise and creative SMS messages."
        },
        {
            "role": "user",
            "content": (
                f"Generate a concise SMS message addressed to {lead_first_name}. "
                f"Include a friendly greeting and incorporate the following content: {message_content}. "
                f"Mention the sender's brokerage, {user.brokerage_name}, appropriately. "
                f"Sign off the message using the sender's first name: {sender_first_name}. "
                "Do not include any placeholder text such as [brokerage_name] or invent additional contact details."
            )
        }
    ]

    completion = client.chat.completions.create(
        model="gpt-4o",
        temperature=0.4,
        messages=messages
    )
    sms_text = completion.choices[0].message.content.strip()
    return sms_text


def draft_email_message_via_ai(user, lead, message_content):
    """
    Uses the OpenAI Chat API to generate a professional HTML-formatted email message.

    The email should:
      - Begin with a greeting like: "Hello, this is <b>{user.name}</b> from <b>{user.brokerage_name}</b>."
      - Address the email to the lead by name.
      - Incorporate the provided message content.

    Ensure no placeholder text (e.g., [brokerage_name]) is used and do not invent additional contact details.
    """
    messages = [
        {
            "role": "system",
            "content": "You are a professional email assistant that drafts creative, HTML-formatted email messages."
        },
        {
            "role": "user",
            "content": (
                f"Generate an HTML-formatted email message that begins with a greeting like: "
                f"'Hello, this is <b>{user.name}</b> from <b>{user.brokerage_name}</b>.' "
                f"Address the email to {lead.name} and incorporate the following content: {message_content}. "
                "Ensure no placeholder text (such as [brokerage_name]) is used and do not invent additional contact details."
            )
        }
    ]

    completion = client.chat.completions.create(
        model="gpt-4o",
        temperature=0.4,
        messages=messages
    )
    email_text = completion.choices[0].message.content.strip()
    cleaned_message = clean_generated_email(email_text)
    return cleaned_message


def draft_email_via_ai(user, meeting_type, meeting_time, additional_description, meeting_details):
    """
    Uses the OpenAI Chat API to generate a custom, professional HTML-formatted email invitation.

    The invitation should:
      - Begin with a greeting like: "Hello, this is <b>{user.name}</b> from <b>{user.brokerage_name}</b>."
      - Clearly state the meeting type and time.
      - Include any additional meeting details provided.
      - Emphasize key details using <b> tags.

    Do not use any placeholder text (such as [brokerage_name]) and do not invent extra contact details.
    """
    messages = [
        {
            "role": "system",
            "content": "You are a professional email assistant who drafts HTML-formatted email invitations."
        },
        {
            "role": "user",
            "content": (
                f"Draft a professional HTML-formatted email invitation for a {meeting_type} meeting with the following details: {meeting_details}. "
                f"Begin with a greeting like: 'Hello, this is <b>{user.name}</b> from <b>{user.brokerage_name}</b>.' "
                f"Then mention that we are scheduling a {meeting_type} meeting at {meeting_time.strftime('%I:%M %p on %B %d, %Y')}. "
                f"Incorporate the following additional details: {additional_description}. "
                "Ensure key details are emphasized using <b> tags, and do not include any placeholder text such as [brokerage_name]."
            )
        }
    ]

    completion = client.chat.completions.create(
        model="gpt-4o",
        temperature=0.4,
        messages=messages
    )
    message_content = completion.choices[0].message.content.strip()
    cleaned_message = clean_generated_email(message_content)
    subject = f"{meeting_type.capitalize()} Meeting Invitation"
    return {"subject": subject, "body": cleaned_message}


@router.post("/book_appointment")
async def book_appointment(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    try:
        data = await request.json()
        required_fields = ['user_id', 'lead_name', 'start_time']
        if not all(field in data for field in required_fields):
            raise HTTPException(
                status_code=400,
                detail={
                    "status": "error",
                    "message": "Missing required fields. Please provide user_id, lead_name, and start_time.",
                    "required_fields": required_fields
                }
            )

        # Query for the matching lead
        matching_leads = db.query(Lead).filter(
            Lead.user_id == data['user_id'],
            Lead.name.ilike(f"%{data['lead_name']}%")
        ).all()

        if not matching_leads:
            return JSONResponse(
                status_code=404,
                content={
                    "status": "error",
                    "message": f"No leads found with the name '{data['lead_name']}'.",
                    "context_for_llm": f"I couldn't find any leads matching the name '{data['lead_name']}' in the database.",
                    "suggestion": "Consider creating a new lead on the dashboard before scheduling an appointment."
                }
            )
        if len(matching_leads) > 1:
            leads_info = [{
                "lead_id": lead.id,
                "name": lead.name,
                "email": lead.email,
                "phone": lead.phone,
                "status": lead.status,
                "source": lead.source
            } for lead in matching_leads]
            return JSONResponse(
                status_code=300,
                content={
                    "status": "multiple_matches",
                    "message": f"Found {len(matching_leads)} leads with the name '{data['lead_name']}'.",
                    "context_for_llm": "Please provide additional information to identify the specific lead.",
                    "matching_leads": leads_info
                }
            )

        lead = matching_leads[0]
        start_time = datetime.fromisoformat(data['start_time'].replace('Z', '+00:00'))
        end_time = start_time + timedelta(hours=1)

        # Prepare meeting details; if no location is provided, assume Google Meet
        meeting_details = {
            "notes": data.get('description', 'Scheduled meeting'),
            "platform": "Google Meet" if not data.get('location') else "In-person",
            # "meeting_id": f"drew_meeting_{hash(str(datetime.utcnow()))}",
            "meeting_link": None,  # Will be updated if a Google Meet is created
            "location": data.get('location')
        }

        def background_booking_process():
            db_bg = SessionLocal()
            try:
                # 1. Create communication and appointment records.
                communication = DrewLeadCommunication(
                    user_id=data['user_id'],
                    lead_id=lead.id,
                    drew_id="agent_drew",
                    type="MEETING",
                    status="SCHEDULED",
                    details=meeting_details
                )
                appointment = Appointment(
                    user_id=data['user_id'],
                    appointment_time=start_time,
                    status="scheduled",
                    participant_details={
                        "lead": {
                            "id": lead.id,
                            "name": lead.name,
                            "meeting_details": meeting_details,
                            "duration": 3600
                        }
                    }
                )
                db_bg.add(communication)
                db_bg.add(appointment)
                db_bg.commit()

                # 2. Retrieve the Google integration credentials.
                email_integration = db_bg.query(Integration).filter_by(
                    user_id=data['user_id'],
                    platform_name='google_calendar'
                ).first()

                # 3. If this is a Google Meet appointment, create a calendar event.
                if meeting_details["platform"] == "Google Meet":
                    credentials = get_user_credentials(data['user_id'])
                    if credentials:
                        credentials = refresh_and_save_credentials(data['user_id'], credentials)
                        service = build('calendar', 'v3', credentials=credentials)
                        event_body = {
                            'summary': f"Meeting with {lead.name}",
                            'description': meeting_details["notes"],
                            'start': {'dateTime': start_time.isoformat(), 'timeZone': 'UTC'},
                            'end': {'dateTime': end_time.isoformat(), 'timeZone': 'UTC'},
                            'conferenceData': {
                                'createRequest': {
                                    'requestId': str(datetime.utcnow().timestamp()),
                                    'conferenceSolutionKey': {'type': 'hangoutsMeet'}
                                }
                            }
                        }
                        event = service.events().insert(
                            calendarId='primary',
                            body=event_body,
                            conferenceDataVersion=1
                        ).execute()
                        meeting_details["meeting_link"] = event.get("hangoutLink")
                        print("Created Google Meet event with link:", meeting_details["meeting_link"])

                        # 4. Retrieve the user (for brokerage details)
                        user = db_bg.query(User).get(data['user_id'])
                        # Ensure that the user object has a 'brokerage_name' attribute.

                        # 5. Call OpenAI to generate a custom email invitation.
                        meeting_type = data.get("meeting_type", "follow-up")
                        drafted_email = draft_email_via_ai(
                            user=user,
                            meeting_type=meeting_type,
                            meeting_details=meeting_details,
                            meeting_time=start_time,
                            additional_description=data.get("description", "")
                        )
                        print("Drafted email from OpenAI:", drafted_email)

                        # 6. Build event details for the email notification.
                        event_details = {
                            "summary": f"Meeting with {lead.name}",
                            "start_time": start_time.isoformat(),
                            "end_time": end_time.isoformat(),
                            # Use the AI-drafted email body.
                            "description": drafted_email.get("body", meeting_details["notes"]),
                            "location": data.get('location', 'Google Meet'),
                            "html_link": meeting_details.get("meeting_link", "")
                        }

                        # 7. Determine the sender email from the integration credentials.
                        sender_email = (
                            email_integration.credentials.get('email')
                            if email_integration and email_integration.credentials.get('email')
                            else 'noreply@example.com'
                        )

                        # 8. Send the drafted email via the Gmail API.
                        send_success = send_email_notification(
                            credentials=credentials,
                            sender_email=sender_email,
                            recipient_email=lead.email,
                            event_details=event_details
                        )
                        if send_success:
                            print("Email notification sent successfully.")
                        else:
                            print("Failed to send email notification.")
                else:
                    # For in-person meetings, add additional notification logic if needed.
                    pass

            except Exception as e:
                db_bg.rollback()
                print(f"Error in background booking process: {e}")
            finally:
                db_bg.close()

        background_tasks.add_task(background_booking_process)

        return JSONResponse(
            status_code=202,
            content={
                "status": "success",
                "message": f"Appointment scheduling initiated with {lead.name}",
                "context_for_llm": (
                    f"I've found the lead '{lead.name}' and started scheduling an appointment for "
                    f"{start_time.strftime('%B %d, %Y at %I:%M %p')}. The appointment will be "
                    f"{'in-person' if data.get('location') else 'via Google Meet'}. Please check your dashboard for notifications."
                ),
                "lead_details": {
                    "lead_id": lead.id,
                    "name": lead.name,
                    "email": lead.email,
                    "status": lead.status
                },
                "appointment_details": {
                    "start_time": start_time.isoformat(),
                    "end_time": end_time.isoformat(),
                    "location": data.get('location', 'Google Meet'),
                    "description": data.get('description', 'Scheduled meeting')
                }
            }
        )
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail={
                "status": "error",
                "message": "Invalid date/time format",
                "context_for_llm": f"There was an error processing the appointment time: {e}. Please ensure the time is in ISO format (YYYY-MM-DDTHH:MM:SS).",
                "error_details": str(e)
            }
        )
    except Exception as e:
        print(f"Error in book_appointment: {e}")
        raise HTTPException(
            status_code=500,
            detail={
                "status": "error",
                "message": "Internal server error",
                "context_for_llm": "An unexpected error occurred while scheduling the appointment. Please try again or contact support.",
                "error_details": str(e)
            }
        )


@router.get("/get_available_times/{user_id}")
async def get_available_times(user_id: int):
    try:
        credentials = get_user_credentials(user_id)
        if not credentials:
            raise HTTPException(
                status_code=401,
                detail={"error": "Google Calendar integration not found", "user_id": user_id}
            )

        credentials = refresh_and_save_credentials(user_id, credentials)
        if not credentials:
            raise HTTPException(
                status_code=401,
                detail={"error": "Failed to refresh credentials", "user_id": user_id}
            )

        service = build('calendar', 'v3', credentials=credentials)
        events = []
        page_token = None
        while True:
            events_result = service.events().list(
                calendarId='primary',
                pageToken=page_token,
                singleEvents=True,
                orderBy='startTime',
                maxResults=2500
            ).execute()
            events.extend(events_result.get('items', []))
            page_token = events_result.get('nextPageToken')
            if not page_token:
                break

        busy_times = []
        for event in events:
            start = event['start'].get('dateTime') or event['start'].get('date')
            end = event['end'].get('dateTime') or event['end'].get('date')
            if start and end:
                busy_slot = {
                    'start': start,
                    'end': end,
                    'summary': event.get('summary', 'Busy'),
                    'id': event.get('id'),
                    'status': event.get('status'),
                    'organizer': event.get('organizer', {}).get('email'),
                    'created': event.get('created'),
                    'updated': event.get('updated'),
                    'attendees_count': len(event.get('attendees', [])),
                    'description': event.get('description', '')
                }
                busy_times.append(busy_slot)

        calendar_data = service.calendars().get(calendarId='primary').execute()
        timezone = calendar_data.get('timeZone', 'UTC')
        busy_times.sort(key=lambda x: x['start'])

        response_data = {
            'busy_times': busy_times,
            'working_hours': {
                'start': '09:00',
                'end': '17:00',
                'timezone': timezone
            },
            'calendar_id': 'primary',
            'timezone': timezone,
            'total_events': len(events)
        }
        return response_data
    except Exception as e:
        import traceback
        print(f"Error in get_available_times: {str(e)}")
        print(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(
            status_code=500,
            detail={"error": "Failed to fetch calendar events", "details": str(e)}
        )


@router.post("/save_communication")
async def save_communication(request: Request, db: Session = Depends(get_db)):
    try:
        data = await request.json()
        required_fields = ['user_id', 'type', 'status', 'details']
        if not all(field in data for field in required_fields):
            raise HTTPException(
                status_code=400,
                detail={"error": "Missing required fields", "required_fields": required_fields}
            )

        user = db.query(User).get(data['user_id'])
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        if data.get('lead_id') and data.get('drew_id'):
            communication = DrewLeadCommunication(
                user_id=data['user_id'],
                lead_id=data['lead_id'],
                drew_id=data['drew_id'],
                type=data['type'],
                status=data['status'],
                details=data['details']
            )
        elif data.get('lead_id'):
            communication = UserLeadCommunication(
                user_id=data['user_id'],
                lead_id=data['lead_id'],
                type=data['type'],
                status=data['status'],
                details=data['details']
            )
        elif data.get('drew_id'):
            communication = UserDrewCommunication(
                user_id=data['user_id'],
                drew_id=data['drew_id'],
                type=data['type'],
                status=data['status'],
                details=data['details']
            )
        else:
            raise HTTPException(status_code=400, detail="Either lead_id or drew_id must be provided")

        db.add(communication)
        call_id = None
        if data['type'].upper() == 'CALL':
            if not all(field in data for field in ['duration', 'call_id']):
                raise HTTPException(
                    status_code=400,
                    detail="Duration and call_id are required for call records"
                )
            call_time = (
                datetime.fromisoformat(data.get('call_time', '').replace('Z', '+00:00'))
                if data.get('call_time')
                else datetime.utcnow()
            )
            call = Call(
                user_id=data['user_id'],
                call_time=call_time,
                status=data['status'],
                duration=data['duration'],
                call_id=data['call_id']
            )
            db.add(call)
            db.flush()  # Flush to generate call.id
            call_id = call.id

        db.commit()
        response = {"status": "success", "communication_id": communication.id}
        if call_id is not None:
            response["call_id"] = call_id
        return JSONResponse(status_code=201, content=response)
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail={"error": "Server error", "details": str(e)}
        )


from sqlalchemy.orm import joinedload  # <-- Add this import at the top

@router.get("/get_user_communications/{user_id}")
async def get_user_communications(user_id: int, start_date: str = None, end_date: str = None,
                                  db: Session = Depends(get_db)):
    try:
        start = datetime.fromisoformat(start_date.replace('Z', '+00:00')) if start_date else None
        end = datetime.fromisoformat(end_date.replace('Z', '+00:00')) if end_date else None

        user = db.query(User).get(user_id)
        if not user:
            raise HTTPException(status_code=404, detail={'status': 'error', 'message': 'User not found'})

        calls_query = db.query(Call).filter_by(user_id=user_id)
        leads_query = db.query(Lead).filter_by(user_id=user_id)
        dlc_query = db.query(DrewLeadCommunication).filter_by(user_id=user_id)
        ulc_query = db.query(UserLeadCommunication).filter_by(user_id=user_id)
        appointments_query = db.query(Appointment).filter_by(user_id=user_id)

        if start:
            calls_query = calls_query.filter(Call.created_at >= start)
            leads_query = leads_query.filter(Lead.created_at >= start)
            dlc_query = dlc_query.filter(DrewLeadCommunication.created_at >= start)
            ulc_query = ulc_query.filter(UserLeadCommunication.created_at >= start)
            appointments_query = appointments_query.filter(Appointment.appointment_time >= start)
        if end:
            calls_query = calls_query.filter(Call.created_at <= end)
            leads_query = leads_query.filter(Lead.created_at <= end)
            dlc_query = dlc_query.filter(DrewLeadCommunication.created_at <= end)
            ulc_query = ulc_query.filter(UserLeadCommunication.created_at <= end)
            appointments_query = appointments_query.filter(Appointment.appointment_time <= end)

        avg_duration = db.query(func.avg(Call.duration)).filter(calls_query.whereclause).scalar() or 0
        call_metrics = {
            'total_calls': calls_query.count(),
            'calls_by_status': {
                'successful': calls_query.filter_by(status='successful').count(),
                'missed': calls_query.filter_by(status='missed').count()
            },
            'average_duration': round(float(avg_duration), 2)
        }

        total_leads = leads_query.count()
        leads_by_status = {status: leads_query.filter_by(status=status).count() for status in
                           ['new', 'contacted', 'qualified', 'closed']}

        all_lead_communications = (
            dlc_query.options(joinedload(DrewLeadCommunication.lead)).all() +
            ulc_query.options(joinedload(UserLeadCommunication.lead)).all()
        )
        all_lead_communications.sort(key=lambda x: x.created_at, reverse=True)
        latest_interactions = [{
            'lead_id': comm.lead_id,
            'lead_name': comm.lead.name if comm.lead else 'Unknown',
            'lead_email': comm.lead.email if comm.lead else None,
            'type': comm.type,
            'status': comm.status,
            'created_at': comm.created_at.isoformat(),
            'details': comm.details,
            'communication_type': type(comm).__name__
        } for comm in all_lead_communications[:5]]

        lead_interaction_counts = {}
        for comm in all_lead_communications:
            lead_interaction_counts[comm.lead_id] = lead_interaction_counts.get(comm.lead_id, 0) + 1
        most_active_lead_id = max(lead_interaction_counts.items(), key=lambda x: x[1])[0] if lead_interaction_counts else None
        most_active_lead = None
        if most_active_lead_id:
            lead_obj = db.query(Lead).get(most_active_lead_id)
            if lead_obj:
                most_active_lead = {
                    'id': lead_obj.id,
                    'name': lead_obj.name,
                    'email': lead_obj.email,
                    'status': lead_obj.status,
                    'interaction_count': lead_interaction_counts[most_active_lead_id],
                    'created_at': lead_obj.created_at.isoformat()
                }

        recent_appointments = appointments_query.order_by(Appointment.appointment_time.desc()).limit(5).all()
        appointments_data = [{
            'id': apt.id,
            'appointment_time': apt.appointment_time.isoformat(),
            'status': apt.status,
            'participant_details': apt.participant_details,
            'created_at': apt.created_at.isoformat()
        } for apt in recent_appointments]

        recent_period = datetime.utcnow() - timedelta(days=30)
        actionable_metrics = {
            'new_leads_last_30_days': leads_query.filter(Lead.created_at >= recent_period).count(),
            'successful_calls_rate': round(
                (call_metrics['calls_by_status']['successful'] / call_metrics['total_calls'] * 100)
                if call_metrics['total_calls'] > 0 else 0, 2
            ),
            'average_interactions_per_lead': round(
                len(all_lead_communications) / total_leads if total_leads > 0 else 0, 2
            ),
            'leads_needing_followup': leads_query.filter(
                Lead.status.in_(['new', 'contacted']),
                Lead.created_at <= datetime.utcnow() - timedelta(days=7)
            ).count(),
            'upcoming_appointments': appointments_query.filter(
                Appointment.appointment_time >= datetime.utcnow(),
                Appointment.status == 'scheduled'
            ).count()
        }

        return {
            'status': 'success',
            'metrics': {
                'call_metrics': call_metrics,
                'lead_metrics': {
                    'total_leads': total_leads,
                    'leads_by_status': leads_by_status,
                    'latest_interactions': latest_interactions,
                    'most_active_lead': most_active_lead
                },
                'appointments': {
                    'recent_appointments': appointments_data,
                    'upcoming_count': actionable_metrics['upcoming_appointments']
                },
                'actionable_metrics': actionable_metrics
            },
            'filters_applied': {
                'start_date': start_date,
                'end_date': end_date
            }
        }
    except Exception as e:
        print(f"Error in get_user_communications: {str(e)}")
        raise HTTPException(status_code=500, detail={'status': 'error', 'message': str(e)})



@router.get("/get_lead_interactions/{lead_id}")
async def get_lead_interactions(lead_id: int, db: Session = Depends(get_db)):
    try:
        drew_lead_comms = db.query(DrewLeadCommunication).filter_by(
            lead_id=lead_id
        ).order_by(DrewLeadCommunication.created_at.asc()).all()
        user_lead_comms = db.query(UserLeadCommunication).filter_by(
            lead_id=lead_id
        ).order_by(UserLeadCommunication.created_at.asc()).all()

        all_communications = sorted(
            drew_lead_comms + user_lead_comms,
            key=lambda x: x.created_at
        )

        interactions = []
        for comm in all_communications:
            date_str = comm.created_at.strftime("%B %d, %Y at %I:%M %p")
            communicator = "Drew" if isinstance(comm, DrewLeadCommunication) else "Agent"
            if comm.type == "CALL":
                notes = comm.details.get('notes', 'No notes available')
                interactions.append(f"[{communicator} Call on {date_str}] {notes}")
            elif comm.type == "EMAIL":
                subject = comm.details.get('subject', 'No subject')
                body = comm.details.get('body', 'No content')
                interactions.append(f"[{communicator} Email sent on {date_str}]\nSubject: {subject}\nContent: {body}")
            elif comm.type == "SMS":
                message = comm.details.get('message', 'No message content')
                interactions.append(f"[{communicator} SMS on {date_str}] {message}")

        lead = db.query(Lead).get(lead_id)
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")

        context = {
            "lead_info": {
                "name": lead.name,
                "email": lead.email,
                "phone": lead.phone,
                "status": lead.status,
                "source": lead.source,
                "lead_details": lead.lead_details
            },
            "interaction_history": interactions,
            "total_interactions": len(interactions),
            "interaction_counts": {
                "drew": len(drew_lead_comms),
                "agent": len(user_lead_comms)
            }
        }
        return context
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": str(e)})


@router.post("/search_leads")
async def search_leads(request: Request, db: Session = Depends(get_db)):
    try:
        data = await request.json()
        print(f"Initial request data: {data}")
        if not data:
            raise HTTPException(status_code=400, detail={"error": "No JSON data provided"})

        user_id = data.get("user_id")
        search_term = data.get("search_term")
        if not user_id or not search_term:
            raise HTTPException(
                status_code=400,
                detail={"error": "Missing required fields: 'user_id' and 'search_term' must be provided."}
            )

        try:
            user_id = int(user_id)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail={"error": "Invalid user_id format. It must be convertible to an integer."}
            )

        matching_leads = db.query(Lead).filter(
            Lead.user_id == user_id,
            Lead.name.ilike(f"%{search_term}%")
        ).all()

        leads_list = []
        for lead in matching_leads:
            lead_data = {
                "lead_id": lead.id,
                "name": lead.name,
                "status": lead.status,
                "email": lead.email,
                "phone": lead.phone,
                "external_id": lead.external_id,
                "source": lead.source,
                "created_at": lead.created_at.isoformat() if lead.created_at else None,
                "updated_at": lead.updated_at.isoformat() if lead.updated_at else None,
                "lead_details": lead.lead_details
            }
            leads_list.append(lead_data)

        print(f"Lead list: {leads_list}")
        return JSONResponse(status_code=200, content=leads_list)
    except Exception as e:
        print(f"Error in /search_leads: {str(e)}")
        raise HTTPException(status_code=500, detail={"error": str(e)})


@router.post("/initiate_call")
async def initiate_call(
        data: Dict[Any, Any],
        background_tasks: BackgroundTasks,
        db: Session = Depends(get_db)
):
    """
    Initiates a call after verifying the contact details.

    Expected JSON payload:
    {
        "user_id": int,            # Required: ID of the user
        "contact_name": str,       # Required: Name of the contact to call
        "call_time": str,          # Required: Scheduled call time in ISO format (YYYY-MM-DDTHH:MM:SS)
        "discussion_points": str   # Optional: Specific details to discuss during the call
    }
    """
    try:
        # Validate required fields
        required_fields = ['user_id', 'contact_name', 'call_time']
        if not all(field in data for field in required_fields):
            return JSONResponse(
                status_code=400,
                content={
                    "status": "error",
                    "message": "Missing required fields. Please provide user_id, contact_name, and call_time.",
                    "required_fields": required_fields
                }
            )

        # Search for contacts (leads) matching the provided contact_name
        matching_contacts = db.query(Lead).filter(
            Lead.user_id == data['user_id'],
            Lead.name.ilike(f"%{data['contact_name']}%")
        ).all()

        # Case 1: No contacts found
        if not matching_contacts:
            return JSONResponse(
                status_code=404,
                content={
                    "status": "error",
                    "message": f"No contacts found with the name '{data['contact_name']}'.",
                    "context_for_llm": (
                        f"I couldn't find any contacts matching the name '{data['contact_name']}' in the database. "
                        "Please verify the contact name or add a new contact first."
                    ),
                    "suggestion": "Consider adding a new contact before initiating a call."
                }
            )

        # Case 2: Multiple contacts found
        if len(matching_contacts) > 1:
            contacts_info = [{
                "contact_id": contact.id,
                "name": contact.name,
                "email": contact.email,
                "phone": contact.phone,
                "status": contact.status,
                "source": contact.source
            } for contact in matching_contacts]

            return JSONResponse(
                status_code=300,
                content={
                    "status": "multiple_matches",
                    "message": f"Found {len(matching_contacts)} contacts with the name '{data['contact_name']}'.",
                    "context_for_llm": (
                        f"I found {len(matching_contacts)} different contacts matching the name '{data['contact_name']}'. "
                        "To avoid confusion, please specify which one you would like to call."
                    ),
                    "matching_contacts": contacts_info,
                    "suggestion": "Please provide additional details to identify the specific contact."
                }
            )

        # Case 3: Exactly one contact found - proceed with call initiation
        contact = matching_contacts[0]

        # Parse and validate call_time
        try:
            call_time = datetime.fromisoformat(data['call_time'].replace('Z', '+00:00'))
        except Exception as e:
            return JSONResponse(
                status_code=400,
                content={
                    "status": "error",
                    "message": "Invalid date/time format for call_time.",
                    "context_for_llm": (
                        "The provided call_time is not in the correct ISO format (YYYY-MM-DDTHH:MM:SS). "
                        "Please provide a valid call time."
                    ),
                    "error_details": str(e)
                }
            )

        # Define background task for call processing
        def background_call_process(db: Session):
            try:
                # Check if any previous Drew–Lead communication exists for this lead.
                existing_comm = db.query(DrewLeadCommunication).filter(
                    DrewLeadCommunication.lead_id == contact.id
                ).first()
                first_interaction = "true" if existing_comm is None else "false"

                # Prepare the details for the communication record (if you still want to store them)
                communication_details = {
                    "notes": data.get('discussion_points', 'Call initiated'),
                    "call_time": call_time.isoformat(),
                }

                # Create a Drew–Lead Communication record for the call
                communication = DrewLeadCommunication(
                    user_id=data['user_id'],
                    lead_id=contact.id,
                    drew_id="agent_drew",  # You may keep this value if needed for your records.
                    type="CALL",
                    status="COMPLETED",
                    details=communication_details
                )
                db.add(communication)

                # Create a Call record
                generated_call_id = f"call_{int(datetime.utcnow().timestamp())}"
                call_record = Call(
                    user_id=data['user_id'],
                    call_time=call_time,
                    status="initiated",
                    duration=0,
                    call_id=generated_call_id
                )
                db.add(call_record)

                db.commit()

                # Retrieve the user record for additional details (like bot and brokerage name)
                user_record = db.query(User).get(data['user_id'])
                override_agent_id = "agent_6467d8b24bd7e6990475ef462b"  # Default value
                if user_record.drew_voice_accent:
                    override_agent_id = user_record.drew_voice_accent.get("outbound_drew_id", override_agent_id)

                # Prepare payload for the webhook POST request, including the first_interaction flag
                webhook_url = "https://services.leadconnectorhq.com/hooks/jyPDXTf3YpjI9G74bRCW/webhook-trigger/46adfe70-c715-405a-9421-ba07be3d2434"
                webhook_headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {os.getenv('GHL_KEY')}"
                }
                payload = {
                    "inbound_dynamic_variables_webhook_url": "https://hook.us2.make.com/5udlpktf98lfq8my2ke6xq8ppiho4x8j",
                    "override_agent_id": override_agent_id,
                    "to_number": str(contact.phone),
                    "from_number": "+14244253466",
                    "operation": "SGL-Outbound-Call",
                    "workflow_status": "active",
                    "retell_llm_dynamic_variables": {
                        "lead_name": contact.name,
                        "lead_id": str(contact.id),
                        "user_id": str(data['user_id']),
                        "bot_name": getattr(user_record, "drew_name", "N/A"),
                        "brokerage_name": getattr(user_record, "brokerage_name", "N/A"),
                        "communication_id": str(communication.id),
                        "additional_info": data.get('discussion_points', ''),
                        "first_interaction": first_interaction
                    }
                }
                response = requests.post(webhook_url, headers=webhook_headers, json=payload)
                print("Webhook response status:", response.status_code, response.text)

            except Exception as e:
                db.rollback()
                print(f"Error in background call process: {str(e)}")
            finally:
                db.close()

        # Add background task (using a new SessionLocal instance)
        background_tasks.add_task(background_call_process, SessionLocal())

        # Prepare the immediate response
        now = datetime.now()
        if call_time <= now:
            response_message = f"I'm calling {contact.name} now."
        else:
            response_message = f"Call scheduled with {contact.name} for {call_time.strftime('%B %d, %Y at %I:%M %p')}."

        context_for_llm = (
            f"I found the contact '{contact.name}' and initiated the call. "
            f"The call is {'starting immediately' if call_time <= now else 'scheduled'}; "
            "the process is running in the background and will update once completed."
        )

        return JSONResponse(
            status_code=202,
            content={
                "status": "success",
                "message": response_message,
                "context_for_llm": context_for_llm,
                "contact_details": {
                    "contact_id": contact.id,
                    "name": contact.name,
                    "email": contact.email,
                    "phone": contact.phone,
                    "status": contact.status
                },
                "call_details": {
                    "call_time": call_time.isoformat(),
                    "discussion_points": data.get('discussion_points', '')
                }
            }
        )

    except Exception as e:
        print(f"Error in initiate_call: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail={
                "status": "error",
                "message": "Internal server error.",
                "context_for_llm": (
                    "I encountered an unexpected error while trying to initiate the call. "
                    "Please try again or contact support if the issue persists."
                ),
                "error_details": str(e)
            }
        )



@router.post("/send_message")
async def send_message(
        data: Dict[Any, Any],
        background_tasks: BackgroundTasks,
        db: Session = Depends(get_db)
):
    """
    Send a message (SMS or Email) to a lead after verifying the lead details.

    Expected JSON payload:
    {
        "user_id": int,           # Required: ID of the user
        "lead_name": str,         # Required: Name of the lead to send the message to
        "message_type": str,      # Required: Type of message; allowed values: "SMS" or "Email"
        "message_content": str    # Required: The content of the message
    }
    """
    try:
        # Validate required fields
        required_fields = ['user_id', 'lead_name', 'message_type', 'message_content']
        if not all(field in data for field in required_fields):
            return JSONResponse(
                status_code=400,
                content={
                    "status": "error",
                    "message": "Missing required fields. Please provide user_id, lead_name, message_type, and message_content.",
                    "required_fields": required_fields
                }
            )

        # Validate message_type
        if data['message_type'].upper() not in ['SMS', 'EMAIL']:
            return JSONResponse(
                status_code=400,
                content={
                    "status": "error",
                    "message": "Invalid message_type. Allowed values are 'SMS' or 'Email'."
                }
            )

        # Search for leads
        matching_leads = db.query(Lead).filter(
            Lead.user_id == data['user_id'],
            Lead.name.ilike(f"%{data['lead_name']}%")
        ).all()

        # Case 1: No leads found
        if not matching_leads:
            return JSONResponse(
                status_code=404,
                content={
                    "status": "error",
                    "message": f"No leads found with the name '{data['lead_name']}'.",
                    "context_for_llm": f"I couldn't find any leads matching the name '{data['lead_name']}' in the database. Please verify the lead name or create a new lead first.",
                    "suggestion": "Consider creating a new lead before sending a message."
                }
            )

        # Case 2: Multiple leads found
        if len(matching_leads) > 1:
            leads_info = [{
                "lead_id": lead.id,
                "name": lead.name,
                "email": lead.email,
                "phone": lead.phone,
                "status": lead.status,
                "source": lead.source
            } for lead in matching_leads]
            return JSONResponse(
                status_code=300,
                content={
                    "status": "multiple_matches",
                    "message": f"Found {len(matching_leads)} leads with the name '{data['lead_name']}'.",
                    "context_for_llm": f"I found multiple leads matching the name '{data['lead_name']}'. Please specify which lead to send the message to.",
                    "matching_leads": leads_info,
                    "suggestion": "Provide additional information to uniquely identify the target lead."
                }
            )

        # Case 3: Exactly one lead found
        lead = matching_leads[0]

        # Define background task for message processing
        def background_message_process(db: Session):
            try:
                # Create a communication record for the message
                message_details = {
                    "message_content": data['message_content'],
                    "message_type": data['message_type'].upper(),
                    "timestamp": datetime.utcnow().isoformat()
                }
                communication = DrewLeadCommunication(
                    user_id=data['user_id'],
                    lead_id=lead.id,
                    drew_id="agent_drew",
                    type=data['message_type'].upper(),
                    status="sent",
                    details=message_details
                )
                db.add(communication)
                db.commit()

                # Retrieve the user record to get details like name, brokerage, and phone number
                user_record = db.query(User).get(data['user_id'])

                if data['message_type'].upper() == "SMS":
                    # Generate a professional SMS message using AI with additional context
                    sms_message = draft_sms_via_ai(user_record, lead, data['message_content'])

                    webhook_headers = {
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {os.getenv('GHL_KEY')}"
                    }

                    # Prepare payload for the SMS webhook request
                    sms_webhook_url = 'https://services.leadconnectorhq.com/hooks/jyPDXTf3YpjI9G74bRCW/webhook-trigger/d5978962-4b47-4631-b34d-61253108974e'
                    sms_payload = {
                        "contact_no": lead.phone,
                        "message": sms_message
                    }
                    # Send the POST request to the SMS webhook
                    sms_response = requests.post(sms_webhook_url, headers=webhook_headers,
                                                 json=sms_payload)
                    print("SMS webhook response:", sms_response.status_code, sms_response.text)

                elif data['message_type'].upper() == "EMAIL":
                    # Generate a professional HTML email message using AI with additional context
                    email_message = draft_email_message_via_ai(user_record, lead, data['message_content'])

                    # Build email event details for the email-sending function
                    event_details = {
                        "summary": f"Message from {user_record.name}",
                        "start_time": datetime.utcnow().isoformat(),
                        "end_time": (datetime.utcnow() + timedelta(hours=1)).isoformat(),
                        "description": email_message,
                        "location": "",  # Not used for generic email messages
                        "html_link": ""
                    }
                    # Retrieve integration credentials for email sending (e.g., Gmail integration)
                    credentials = get_user_credentials(data['user_id'])
                    sender_email = "noreply@example.com"
                    if credentials:
                        credentials = refresh_and_save_credentials(data['user_id'], credentials)
                        integration_record = db.query(Integration).filter_by(user_id=data['user_id'],
                                                                             platform_name="google_calendar").first()
                        if integration_record and integration_record.credentials.get("email"):
                            sender_email = integration_record.credentials.get("email")
                    # Send the email using your email-sending function
                    send_success = send_email_notification(credentials, sender_email, lead.email,
                                                           event_details)
                    print("Email sending status:", send_success)

                # Log that the message was processed
                print(f"Message sent to {lead.name}: {data['message_content']} ({data['message_type'].upper()})")
            except Exception as e:
                db.rollback()
                print(f"Error in background message process: {str(e)}")

        # Add background task (using a new SessionLocal instance)
        background_tasks.add_task(background_message_process, SessionLocal())

        # Prepare immediate response
        response_message = f"Message sending initiated to {lead.name}."
        context_for_llm = (
            f"I found the lead '{lead.name}' and initiated sending a {data['message_type'].upper()} message. "
            "The process is running in the background and will update once completed."
        )

        return JSONResponse(
            status_code=202,
            content={
                "status": "success",
                "message": response_message,
                "context_for_llm": context_for_llm,
                "lead_details": {
                    "lead_id": lead.id,
                    "name": lead.name,
                    "email": lead.email,
                    "phone": lead.phone,
                    "status": lead.status
                },
                "message_details": {
                    "message_type": data['message_type'].upper(),
                    "message_content": data['message_content']
                }
            }
        )

    except Exception as e:
        print(f"Error in send_message endpoint: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail={
                "status": "error",
                "message": "Internal server error.",
                "error_details": str(e)
            }
        )
