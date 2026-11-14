
import json

from uuid import uuid4
from typing import Literal

from pydantic import BaseModel, Field

from models import AgentMessage


# =====================================
# Agent Message Structure
# =====================================

class Message(BaseModel):

    sender: str

    receiver: str

    performative: Literal[
        "REQUEST",
        "AGREE",
        "REFUSE",
        "PROPOSE",
        "INFORM",
        "FAILURE"
    ]

    content: dict

    conversation_id: str = Field(
        default_factory=lambda: str(uuid4())
    )


# =====================================
# Create Agent Response
# =====================================

def reply(message, performative, content):

    return Message(

        sender=message.receiver,

        receiver=message.sender,

        performative=performative,

        content=content,

        conversation_id=message.conversation_id

    )


# =====================================
# Message Bus
# =====================================

class MessageBus:

    def __init__(self):

        self.agents = {}


    # Register Agent

    def register(self, name, agent):

        self.agents[name] = agent


    # =================================
    # Save Agent Message to Database
    # =================================

    def log(self, message, db):

        record = AgentMessage(

            sender=message.sender,

            receiver=message.receiver,

            performative=message.performative,

            content=json.dumps(
                message.content,
                default=str
            ),

            conversation_id=message.conversation_id

        )

        db.add(record)


    # =================================
    # Send Message to Specific Agent
    # =================================

    def send(self, message, db):

        # Serialize and deserialize JSON

        message = Message.model_validate_json(
            message.model_dump_json()
        )


        # Log Incoming Message

        self.log(message, db)


        # Find Receiver

        agent = self.agents.get(
            message.receiver
        )


        # Handle Unknown Agent

        if agent is None:

            response = reply(

                message,

                "FAILURE",

                {
                    "reason": (
                        f"Agent {message.receiver} "
                        "not found"
                    )
                }

            )

            self.log(response, db)

            return response


        # Deliver Message

        response = agent.receive(
            message,
            db
        )


        # Validate Response

        if not isinstance(response, Message):

            response = reply(

                message,

                "FAILURE",

                {
                    "reason": "Invalid agent response"
                }

            )


        # Log Response

        self.log(response, db)

        return response


    # =================================
    # Broadcast Message to All Agents
    # Supports Old and New Calling Methods
    # =================================

    def broadcast(
        self,
        sender,
        content=None,
        db=None,
        conversation_id=None
    ):

        # New calling method:
        # bus.broadcast(message, db)

        if isinstance(sender, Message):

            message = sender

            if db is None:

                db = content


        # Old calling method:
        # bus.broadcast(sender, content,
        #               db, conversation_id)

        else:

            message = Message(

                sender=sender,

                receiver="*",

                performative="INFORM",

                content=content or {},

                conversation_id=(
                    conversation_id or str(uuid4())
                )

            )


        if db is None:

            raise ValueError(
                "Database session is required"
            )


        responses = []


        # Deliver Message to All Other Agents

        for agent_name in list(self.agents):

            if agent_name == message.sender:

                continue

            broadcast_message = Message(

                sender=message.sender,

                receiver=agent_name,

                performative="INFORM",

                content=message.content,

                conversation_id=message.conversation_id

            )

            response = self.send(

                broadcast_message,

                db

            )

            responses.append(response)


        return responses


# =====================================
# Global Message Bus Instance
# =====================================

bus = MessageBus()
