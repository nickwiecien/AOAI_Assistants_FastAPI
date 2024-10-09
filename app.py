from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv
import os
import base64
from pydantic import BaseModel
import threading
import queue
import tempfile

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

class FileUploadRequest(BaseModel):
    file_name: str
    file_data: str  # This will hold the base64-encoded file data
    thread_id: str

@app.post("/upload_file_and_create_thread")
async def upload_file_and_create_thread(request: Request):
    try:
        body = await request.json()
        file_name = body.get("file_name")
        file_data = body.get("file_data")
        file_bytes = base64.b64decode(file_data)

        thread = None
        file = None

        # Use a temporary directory
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_file_path = os.path.join(temp_dir, file_name)

            # Save the file in the temp directory
            with open(temp_file_path, "wb") as temp_file:
                temp_file.write(file_bytes)

            # Open the file using a with statement to ensure it closes properly
            with open(temp_file_path, 'rb') as f:
                file = client.files.create(
                    file=f,
                    purpose='assistants'
                )

            # No need to manually delete the temp file; it will be deleted automatically

            thread = client.beta.threads.create(
                messages=[
                    {
                        "role": "user",
                        "content": f"Uploaded file: {file_name}",
                        "file_ids": [file.id]
                    }
                ]
            )

        return thread.id

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to upload file: {str(e)}")

@app.post("/run_assistant")
async def run_assistant(request: Request):
    # Extract thread_id and message from the request
    body = await request.json()
    thread_id = body.get("thread_id")
    user_message = body.get("message")
    count = 0

    def generate_response():
        q = queue.Queue()

        # Define the EventHandler class
        class EventHandler(AssistantEventHandler):
            def __init__(self, client):
                super().__init__()  # Call the parent constructor
                self.queue = q
                self.client = client
                self.status = ''
                self.tool_call_active = False

            @override
            def on_text_created(self, text) -> None:
                # Handle text generation
                pass

            @override
            def on_tool_call_created(self, tool_call):
                if self.status!= 'toolcall_created':
                    self.status = 'toolcall_created'
                    self.tool_call_active = True
                if tool_call.type == 'code_interpreter':
                    self.queue.put('<i>Launching Code Interpreter...</i>\n')
                    self.queue.put("<pre><code>")

            @override
            def on_tool_call_delta(self, delta, snapshot) -> None:
                if self.status!= 'toolcall_delta':
                    self.status = 'toolcall_delta'
                if delta.type == 'code_interpreter':
                    if self.tool_call_active==False:
                        self.queue.put('<pre><code>')
                        self.tool_call_active = True
                    if delta.code_interpreter.input:
                        self.queue.put(delta.code_interpreter.input)

                    if delta.code_interpreter.outputs:
                        for output in delta.code_interpreter.outputs:
                            if output.type == "logs":
                                self.queue.put(f"\n{output.logs}\n")

            @override
            def on_tool_call_done(self, tool_call) -> None:
                if self.status!= 'toolcall_done':
                    self.status = 'toolcall_done'
                if tool_call.type == 'code_interpreter':
                    self.queue.put('</code></pre>')
                    self.queue.put('\n')
                    self.tool_call_active = False

            

            @override
            def on_message_created(self, message) -> None:
                if self.status!= 'message_created':
                    self.status = 'message_created'
                pass

            @override
            def on_message_delta(self, delta, snapshot) -> None:
                if self.status!= 'message_delta':
                    self.status = 'message_delta'
                for content in delta.content:
                    if content.type == 'image_file':
                        img_bytes = self.client.files.content(content.image_file.file_id).read()
                        # Encode image as base64 and send as data URL
                        encoded_image = base64.b64encode(img_bytes).decode('utf-8')
                        data_url = f'<img width="750px" src="data:image/png;base64,{encoded_image}"/><br>'
                        self.queue.put(data_url)
                        self.queue.put('\n')
                        self.queue.put('\n')
                    elif content.type == 'text':
                        self.queue.put(content.text.value)

            @override
            def on_message_done(self, message) -> None:
                if self.status!= 'message_done':
                    self.status = 'message_done'
                self.queue.put('\n')
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
            item_with_line_breaks = item
            yield item_with_line_breaks

        sdk_thread.join()

    return StreamingResponse(generate_response(), media_type="text/plain")