from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import time
from dotenv import load_dotenv
import os
import base64
from pydantic import BaseModel
import threading
import queue
import json

app = FastAPI()

load_dotenv()

from typing_extensions import override
from openai import AssistantEventHandler, OpenAI, AzureOpenAI

global client, assistant

client = AzureOpenAI(
            azure_endpoint=os.environ["AOAI_ENDPOINT"],
            api_key=os.environ["AOAI_KEY"],
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview"),
        )

assistant = client.beta.assistants.retrieve(
            os.environ['AOAI_ASSISTANT_ID']
        )


@app.post("/create_thread")
def create_thread():
    thread = client.beta.threads.create()
    return thread.id

@app.post("/run_assistant")
async def run_assistant(request: Request):
    # Extract thread_id and message from the request
    body = await request.json()
    thread_id = body.get("thread_id")
    user_message = body.get("message")

    def generate_response():
        q = queue.Queue()

        # Define the EventHandler class
        class EventHandler(AssistantEventHandler):
            def __init__(self, client):
                super().__init__()  # Call the parent constructor
                self.queue = q
                self.client = client

            @override
            def on_text_created(self, text) -> None:
                # Handle text generation
                pass

            @override
            def on_tool_call_created(self, tool_call):
                if tool_call.type == 'code_interpreter':
                    self.queue.put('<i>Launching Code Interpreter...</i>\n ``` ')

            @override
            def on_tool_call_delta(self, delta, snapshot) -> None:
                if delta.type == 'code_interpreter':
                    if delta.code_interpreter.input:
                        self.queue.put(delta.code_interpreter.input)

                    if delta.code_interpreter.outputs:
                        self.queue.put('\n ``` \n')
                        for output in delta.code_interpreter.outputs:
                            if output.type == "logs":
                                self.queue.put(f"\n{output.logs}\n")

            @override
            def on_message_created(self, message) -> None:
                pass

            @override
            def on_message_delta(self, delta, snapshot) -> None:
                for content in delta.content:
                    if content.type == 'image_file':
                        img_bytes = self.client.files.content(content.image_file.file_id).read()
                        # Encode image as base64 and send as data URL
                        encoded_image = base64.b64encode(img_bytes).decode('utf-8')
                        data_url = f'<img width="750px" src="data:image/png;base64,{encoded_image}"/><br><br>'
                        self.queue.put(data_url)
                    elif content.type == 'text':
                        self.queue.put(content.text.value)

            @override
            def on_message_done(self, message) -> None:
                pass

        # Function to run the SDK code
        def run_assistant_code():

            # Send the user message
            client.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=user_message,
            )

            handler = EventHandler(client)

            # Use the stream SDK helper with the EventHandler
            with client.beta.threads.runs.stream(
                thread_id=thread_id,
                assistant_id=assistant.id,
                event_handler=handler,
            ) as stream:
                stream.until_done()

            # Indicate completion
            q.put(None)

        # Start the SDK code in a separate thread
        sdk_thread = threading.Thread(target=run_assistant_code)
        sdk_thread.start()

        # Read items from the queue and yield them
        while True:
            item = q.get()
            if item is None:
                break
            # Replace newline characters with <br> tags
            item_with_line_breaks = item.replace('\n', '<br>')
            item_with_line_breaks = item
            # Encode the item as JSON
            json_data = json.dumps({'content': item_with_line_breaks})
            yield f"data: {json_data}\n\n"

        sdk_thread.join()

    return StreamingResponse(generate_response(), media_type="text/plain")