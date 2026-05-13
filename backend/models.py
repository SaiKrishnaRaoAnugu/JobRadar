from sqlalchemy import Column, Integer, String, Text, DateTime, Float, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from database import Base


class User(Base):
    __tablename__ = "users"

    id         = Column(Integer, primary_key=True, index=True)
    google_id  = Column(String, unique=True, nullable=False)
    email      = Column(String, unique=True, nullable=False)
    name       = Column(String, default="")
    picture    = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)

    applications = relationship("Application", back_populates="user", cascade="all, delete-orphan")


class Application(Base):
    __tablename__ = "applications"

    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(Integer, ForeignKey("users.id"), nullable=False)
    job_title    = Column(String, nullable=False)
    company      = Column(String, default="")
    url          = Column(String, nullable=False)
    location     = Column(String, default="")
    source       = Column(String, default="")
    salary_min   = Column(Float, nullable=True)
    salary_max   = Column(Float, nullable=True)
    status       = Column(String, default="applied")
    notes        = Column(Text, default="")
    applied_date = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="applications")
