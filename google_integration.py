# google_integration.py
import os
import base64
import httplib2
from datetime import datetime
from datetime import timezone
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import Request as GoogleAuthRequest
from google_auth_oauthlib.flow import Flow
import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from googleapiclient.discovery import build
from config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, REDIRECT_URI, SCOPES
from models import Integration, IntegrationStatus
from database import SessionLocal


def create_flow():
    try:
        return Flow.from_client_secrets_file(
            'client_secrets.json',
            scopes=SCOPES,
            redirect_uri=REDIRECT_URI
        )
    except FileNotFoundError:
        print("client_secrets.json not found, using environment variables")
        client_config = {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [REDIRECT_URI]
            }
        }
        print("Client Config:", client_config)
        return Flow.from_client_config(client_config, scopes=SCOPES)


def save_integration_to_db(credentials, email, user_id):
    """
    Save or update the integration credentials and status for a given user.
    """
    db = SessionLocal()
    try:
        credentials_dict = {
            'token': credentials.token,
            'refresh_token': credentials.refresh_token,
            'token_uri': credentials.token_uri,
            'client_id': credentials.client_id,
            'client_secret': credentials.client_secret,
            'scopes': credentials.scopes,
            'expiry': credentials.expiry.isoformat() if credentials.expiry else None,
            'email': email
        }
        with db.begin():
            integration = db.query(Integration).filter_by(
                user_id=user_id,
                platform_name='google_calendar'
            ).first()
            if not integration:
                integration = Integration(
                    user_id=user_id,
                    platform_name='google_calendar',
                    credentials=credentials_dict
                )
                db.add(integration)
            else:
                integration.credentials = credentials_dict

            integration_status = db.query(IntegrationStatus).filter_by(
                user_id=user_id,
                platform_name='google_calendar'
            ).first()
            if not integration_status:
                integration_status = IntegrationStatus(
                    user_id=user_id,
                    platform_name='google_calendar',
                    status='active'
                )
                db.add(integration_status)
            else:
                integration_status.status = 'active'
                integration_status.last_checked = datetime.utcnow()
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"Error saving integration to database: {e}")
        raise
    finally:
        db.close()


def get_user_credentials(user_id, platform_name='google_calendar'):
    """
    Retrieve the stored Google credentials for the given user.
    """
    db = SessionLocal()
    try:
        integration = db.query(Integration).filter_by(
            user_id=user_id,
            platform_name=platform_name
        ).first()
        if not integration or not integration.credentials:
            return None
        creds_data = integration.credentials
        credentials = Credentials(
            token=creds_data['token'],
            refresh_token=creds_data['refresh_token'],
            token_uri=creds_data['token_uri'],
            client_id=creds_data['client_id'],
            client_secret=creds_data['client_secret'],
            scopes=creds_data['scopes']
        )
        if 'expiry' in creds_data and creds_data['expiry']:
            dt = datetime.fromisoformat(creds_data['expiry'].replace('Z', '+00:00'))
            # Convert to UTC and remove timezone information to get a naive datetime
            credentials.expiry = dt.astimezone(timezone.utc).replace(tzinfo=None)

        return credentials
    except Exception as e:
        print(f"Error retrieving credentials from database: {e}")
        return None
    finally:
        db.close()


def refresh_and_save_credentials(user_id, credentials, platform_name='google_calendar'):
    """
    Refresh the credentials if expired and update them in the database.
    """
    db = SessionLocal()
    try:
        if not credentials:
            print(f"No credentials provided for user {user_id}")
            return None
        if not credentials.expired:
            print(f"Credentials for user {user_id} are not expired")
            return credentials
        if not credentials.refresh_token:
            print(f"No refresh token available for user {user_id}")
            return None

        http_obj = httplib2.Http()
        try:
            credentials.refresh(GoogleAuthRequest(http_obj))
        except Exception as refresh_error:
            print(f"Credential refresh failed for user {user_id}: {str(refresh_error)}")
            print(f"Existing token details: {credentials.__dict__}")
            return None

        integration = db.query(Integration).filter_by(
            user_id=user_id,
            platform_name=platform_name
        ).first()
        if integration:
            try:
                integration.credentials = {
                    'token': credentials.token,
                    'refresh_token': credentials.refresh_token,
                    'token_uri': credentials.token_uri,
                    'client_id': credentials.client_id,
                    'client_secret': credentials.client_secret,
                    'scopes': credentials.scopes,
                    'expiry': credentials.expiry.isoformat() if credentials.expiry else None
                }
                db.commit()
            except Exception as db_error:
                print(f"Failed to update credentials in database for user {user_id}: {str(db_error)}")
                db.rollback()
        return credentials
    except Exception as e:
        print(f"Unexpected error in credential refresh for user {user_id}: {str(e)}")
        return None
    finally:
        db.close()


def send_email_notification(credentials, sender_email, recipient_email, event_details):
    """
    Compose and send an email notification using the drafted email content.
    The plain-text part is attached first and the HTML part second so that HTML-capable
    email clients render the HTML instead of showing raw markup.
    """
    try:
        # Create a multipart/alternative message container.
        message = MIMEMultipart("alternative")
        message["to"] = recipient_email
        message["from"] = sender_email
        message["subject"] = f"Calendar Invitation: {event_details['summary']}"

        # 1. Build the plain-text version.
        # (This is a fallback in case the email client does not support HTML.)
        text_content = (
            f"Calendar Invitation: {event_details['summary']}\n\n"
            f"Details:\n{event_details.get('description', 'No additional details provided.')}\n\n"
            f"Meeting Link: {event_details.get('html_link', 'N/A')}\n\n"
            "This is an automated message. Please do not reply."
        )
        text_part = MIMEText(text_content, "plain")
        message.attach(text_part)

        # 2. Build the HTML version.
        html_content = (
            "<!DOCTYPE html>"
            "<html>"
            "<head><meta charset='UTF-8'></head>"
            "<body style='font-family: Arial, sans-serif;'>"
            f"{event_details.get('description', 'No additional details provided.')}"
            "</body>"
            "</html>"
        )
        html_part = MIMEText(html_content, "html")
        message.attach(html_part)

        # 3. Encode and send the email.
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        gmail_service = build("gmail", "v1", credentials=credentials)
        sent_message = gmail_service.users().messages().send(
            userId="me", body={"raw": raw_message}
        ).execute()

        return True
    except Exception as e:
        print(f"Error sending email: {e}")
        return False
