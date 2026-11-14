import json
from uuid import uuid4
from typing import Literal
from pydantic import BaseModel, Field
from models import AgentMessage


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


class MessageBus:

    def __init__(self):
        self.agents = {}

    def register(self, name, agent):
        self.agents[name] = agent

    def log(self, message, db):

        record = AgentMessage(
            sender=message.sender,
            receiver=message.receiver,
            performative=message.performative,
            content=json.dumps(message.content),
            conversation_id=message.conversation_id
        )

        db.add(record)

    def send(self, message, db):

        # Exchange a serialized JSON message
        message = Message.model_validate_json(
            message.model_dump_json()
        )

        self.log(message, db)

        agent = self.agents.get(message.receiver)

        if agent is None:
            response = Message(
                sender="MessageBus",
                receiver=message.sender,
                performative="FAILURE",
                content={"reason": "Agent not found"},
                conversation_id=message.conversation_id
            )
        else:
            response = agent.receive(message, db)

        self.log(response, db)

        return response

    def broadcast(self, sender, content, db, conversation_id):

        for name in self.agents:

            if name == sender:
                continue

            message = Message(
                sender=sender,
                receiver=name,
                performative="INFORM",
                content=content,
                conversation_id=conversation_id
            )

            self.send(message, db)


def reply(message, performative, content):

    return Message(
        sender=message.receiver,
        receiver=message.sender,
        performative=performative,
        content=content,
        conversation_id=message.conversation_id
    )


bus = MessageBus()