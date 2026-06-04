# Magicplan Project Exporter

A Python script that connects to the Magicplan API, lists available projects, lets the user select a project, fetches the project data, and saves the result locally as a JSON file.

## Features

* Lists Magicplan projects available to the configured workspace
* Lets the user select a project from the terminal
* Fetches project information from the Magicplan API
* Attempts to fetch related plan data, including rooms, walls, objects, doors, and windows when available
* Displays the fetched data in the terminal
* Saves the exported result locally as a JSON file

## Requirements

* Python 3.10 or higher
* Magicplan API credentials:

  * Customer ID
  * API Key

## Setup

Clone the repository:

```bash
git clone https://github.com/YOUR_USERNAME/magicplan-project-exporter.git
cd magicplan-project-exporter
```

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Create a local environment file:

```bash
cp .env.example .env
```

Open `.env` and add your Magicplan credentials:

```env
MAGICPLAN_CUSTOMER_ID=your_customer_id_here
MAGICPLAN_API_KEY=your_api_key_here
MAGICPLAN_BASE_URL=https://cloud.magicplan.app/api/v2
MAGICPLAN_ACCEPT_LANGUAGE=en-US
```

Do not commit your `.env` file to GitHub.

## Usage

Run the script:

```bash
python main.py
```

The script will:

1. Check the Magicplan workspace connection.
2. Fetch the list of available projects.
3. Display the projects in the terminal.
4. Ask the user to select a project.
5. Fetch the selected project’s data.
6. Attempt to fetch related plan data.
7. Display the fetched JSON.
8. Save the result locally as a JSON file.

## Output

Exported project data is saved locally in the output folder.

Example output path:

```text
.local_storage/magicplan_project_bundle_<project_id>_<timestamp>.json
```

The exported JSON may include:

* Project metadata
* Plan data
* Floors
* Rooms
* Walls
* Objects
* Doors
* Windows
* API warnings, if some data could not be fetched

## Project Structure

```text
magicplan-project-exporter/
├── .env.example
├── .gitignore
├── README.md
├── requirements.txt
├── main.py
└── magicplan_project_fetcher/
    ├── __init__.py
    └── client.py
```

## Notes

The `.env` file is ignored by Git and should only be stored locally.

The script is designed to be easy to configure and run after cloning the repository. Future improvements may include better parsing of Magicplan plan data, cleaner object summaries, and more efficient API/data processing.
