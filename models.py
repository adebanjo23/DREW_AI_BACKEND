from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, JSON
from sqlalchemy.orm import relationship
from database import Base


class User(Base):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False)
    password = Column(String, nullable=False)
    phone = Column(String)
    brokerage_name = Column(String)
    role = Column(String, default='user')
    created_at = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=False)
    is_verified = Column(Boolean, default=False)
    drew_voice_accent = Column(JSON)
    package_id = Column(Integer, ForeignKey('packages.id'))

    # Relationships
    leads = relationship('Lead', backref='user', lazy=True)
    onboarding = relationship('Onboarding', backref='user', uselist=False, lazy=True)
    integration_statuses = relationship('IntegrationStatus', backref='user', lazy=True)


class Lead(Base):
    __tablename__ = 'leads'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    external_id = Column(String(255))
    source = Column(String(50))
    name = Column(String(255))
    email = Column(String(255))
    phone = Column(String(20))
    status = Column(String(50))
    lead_details = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Integration(Base):
    __tablename__ = 'integrations'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    platform_name = Column(String(100))
    credentials = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)


class IntegrationStatus(Base):
    __tablename__ = 'integration_status'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    platform_name = Column(String(100))
    status = Column(String(50))
    last_checked = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Onboarding(Base):
    __tablename__ = 'onboarding'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    current_step = Column(Integer, default=1)
    crm_credentials = Column(JSON)
    scheduling_preferences = Column(JSON)
    communication_tone = Column(String)
    completed = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Appointment(Base):
    __tablename__ = 'appointments'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    appointment_time = Column(DateTime)
    status = Column(String(50))
    participant_details = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationship with User
    user = relationship('User', backref='appointments', lazy=True)


class Call(Base):
    __tablename__ = 'calls'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    call_time = Column(DateTime, default=datetime.utcnow)
    status = Column(String(50))
    duration = Column(Integer)
    call_id = Column(String(50))
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationship with User
    user = relationship('User', backref='calls', lazy=True)


class DrewLeadCommunication(Base):
    __tablename__ = 'drew_lead_communications'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    drew_id = Column(String(50))
    lead_id = Column(Integer, ForeignKey('leads.id', ondelete='SET NULL'))
    type = Column(String(20))
    status = Column(String(20))
    details = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = relationship('User', backref='drew_lead_communications', lazy=True)
    lead = relationship('Lead', backref='drew_communications', lazy=True)


class UserLeadCommunication(Base):
    __tablename__ = 'user_lead_communications'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    lead_id = Column(Integer, ForeignKey('leads.id', ondelete='SET NULL'))
    type = Column(String(20))
    status = Column(String(20))
    details = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = relationship('User', backref='user_lead_communications', lazy=True)
    lead = relationship('Lead', backref='user_communications', lazy=True)


class UserDrewCommunication(Base):
    __tablename__ = 'user_drew_communications'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    drew_id = Column(String(50))
    type = Column(String(20))
    status = Column(String(20))
    details = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = relationship('User', backref='user_drew_communications', lazy=True)
