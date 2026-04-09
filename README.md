# Proposal PDF Flask Server

This folder contains the Flask server that generates proposal PDFs.

## What this server does

- Accepts proposal data
- Generates a PDF
- Returns the PDF as a download

Main file:

- `main.py`

## Requirements

Make sure Python is installed on your system.

Recommended:

- Python 3.10 or newer

## Setup

### 1. Create virtual environment

```powershell
python -m venv venv
```

### 2. Activate virtual environment

```powershell
.\venv\Scripts\Activate.ps1
```

If PowerShell blocks this command, run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

Then activate again:

```powershell
.\venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```powershell
pip install -r requirements.txt
```

## Run the Flask server

```powershell
$env:FLASK_ENV="development"
$env:PORT="5000"
python main.py
```

The server will start on:

```text
http://localhost:5000
```

## Health check

Open this in your browser or test in PowerShell:

```text
http://localhost:5000/
```

PowerShell test:

```powershell
Invoke-RestMethod -Uri "http://localhost:5000/" -Method Get
```

Expected response:

```json
{
  "status": "healthy",
  "service": "Proposal PDF Generation Service"
}
```

## API endpoints

### `GET /`

Health check endpoint.

### `POST /generate-pdf`

Generates a proposal PDF.

Response:

- `200` returns PDF file
- `400` invalid request
- `500` server error

## Important input

The server expects JSON data.

Important field:

- `inventories`

If `inventories` is missing or empty, PDF generation will fail with `400`.

Some commonly used fields are:

- `campaignName`
- `clientName`
- `companyName`
- `inventories`
- `themeColor`
- `primaryColor`
- `fontColor`
- `tableColor`
- `tableFontColor`
- `tagline`
- `logoBase64`
- `headerBase64`
- `templatePDF`
- `backgroundPageIndex`
- `mode`

## Optional environment variables

You can run the server without setting these, but they are supported:

```env
PORT=5000
FLASK_ENV=development
FLASK_HOT_RELOAD=1
PROPOSAL_TMP_DIR=
```

## Useful files

- `main.py` - Flask entry point
- `upload_handler.py` - template page insertion
- `hybrid_handler.py` - hybrid template logic
- `requirements.txt` - Python packages
- `static/` - static image assets

## Troubleshooting

### Port already in use

Change the port before starting:

```powershell
$env:PORT="5001"
python main.py
```

### Dependencies not installing

Upgrade pip first:

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### PDF generation fails

Check:

- the server is running
- the request body is valid JSON
- `inventories` is present and not empty

### Images or maps are not showing

Check:

- image URLs are valid
- base64 image data is valid
- the temp directory is writable
