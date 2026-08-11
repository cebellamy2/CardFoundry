from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="CardFoundry")


@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <!DOCTYPE html>
    <html>
        <head>
            <title>CardFoundry</title>
        </head>
        <body>
            <h1>CardFoundry</h1>
            <p>Your card inventory has a home.</p>
            <p>Version 0.0.1</p>
        </body>
    </html>
    """