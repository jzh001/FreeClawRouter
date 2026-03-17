# FreeClawRouter — Installation Guide

Welcome! This guide walks you through setting up FreeClawRouter from scratch. No technical background needed. It should take about 15 minutes.

---

## 1. What is FreeClawRouter?

FreeClawRouter is a small program that runs on your computer and acts as a smart middleman between your AI assistant (OpenClaw) and the internet. It automatically routes your requests to the best free AI service available — and falls back to a local AI model on your machine if the free services are busy.

The result: a fully functional AI assistant that costs nothing to run.

---

## 2. What you need

Before you start, make sure you have:

- A computer running **macOS**, **Windows 10/11**, or **Linux**
- **Docker Desktop** installed (we will install this in step 3)
- **Ollama** installed (we will install this in step 4)
- About **15 minutes** of free time
- A free account with at least one AI provider (we will set this up in step 6)

---

## 3. Install Docker Desktop

Docker Desktop is the tool that runs FreeClawRouter in an isolated container — you do not need to install Python or any other software manually.

1. Go to [https://www.docker.com/products/docker-desktop/](https://www.docker.com/products/docker-desktop/)
2. Download the version for your operating system
3. Open the downloaded file and follow the on-screen installer
4. Start Docker Desktop and wait for it to say "Docker Desktop is running" (the whale icon in your menu bar or system tray turns solid)

---

## 4. Install Ollama and download the AI model

Ollama runs AI models on your own computer. FreeClawRouter uses it for smart routing decisions and as a fallback when all cloud API quotas are exhausted.

1. Go to [https://ollama.com](https://ollama.com) and download Ollama for your operating system
2. Install it by opening the downloaded file and following the prompts
3. Open a **Terminal** (macOS/Linux) or **Command Prompt** (Windows) and run:

```
ollama pull gpt-oss:20b
```

This downloads a ~12 GB AI model to your computer. It only needs to be done once. Grab a coffee — it may take a few minutes depending on your internet speed.

> **Why on the host instead of inside Docker?** Running Ollama on your machine directly means it can use your GPU (Metal on Apple Silicon, CUDA on NVIDIA) for fast inference, and the model files are shared — no duplicate storage.

---

## 5. Download FreeClawRouter

**Option A — Using Git (recommended if you have it):**

Open a Terminal or Command Prompt and run:

```
git clone https://github.com/your-username/freeclawrouter.git
cd freeclawrouter
```

**Option B — Download a ZIP:**

1. Go to the FreeClawRouter GitHub page
2. Click the green **Code** button, then **Download ZIP**
3. Unzip the downloaded file
4. Open a Terminal or Command Prompt and navigate into the unzipped folder:
   - macOS/Linux: `cd ~/Downloads/freeclawrouter`
   - Windows: `cd C:\Users\YourName\Downloads\freeclawrouter`

---

## 6. Get your free API keys

FreeClawRouter works with several free AI services. You need at least one key to get started — the more you add, the higher your combined daily request limit.

In the FreeClawRouter folder, copy the example environment file:

```
cp .env.example .env
```

On Windows:
```
copy .env.example .env
```

Open the `.env` file with any text editor (Notepad, TextEdit, VS Code, etc.) and fill in the keys for the services you sign up for below.

---

### Cerebras — 14,400 free requests per day

1. Go to [https://cloud.cerebras.ai](https://cloud.cerebras.ai) and click **Sign Up** (no credit card required)
2. After signing in, click your profile icon (top right) → **API Keys**
3. Click **Create new key**, give it a name like "FreeClaw", and copy the key
4. In your `.env` file, paste it next to `CEREBRAS_API_KEY=`

---

### Groq — 14,400 free requests per day

1. Go to [https://console.groq.com](https://console.groq.com) and click **Sign Up** (no credit card required)
2. After signing in, go to **API Keys** in the left sidebar
3. Click **Create API Key**, copy the key
4. In your `.env` file, paste it next to `GROQ_API_KEY=`

---

### Google AI Studio (Gemini) — 250 free requests per day

1. Go to [https://aistudio.google.com](https://aistudio.google.com) and sign in with your Google account
2. Click **Get API key** (top left) → **Create API key**
3. Copy the key that appears
4. In your `.env` file, paste it next to `GOOGLE_AI_API_KEY=`

---

### OpenRouter — 200 free requests per day

1. Go to [https://openrouter.ai](https://openrouter.ai) and click **Sign In** (free account)
2. After signing in, go to **Keys** in the top menu
3. Click **Create Key**, give it a name, and copy it
4. In your `.env` file, paste it next to `OPENROUTER_API_KEY=`

---

### Other providers (optional)

The `.env` file also has slots for **NVIDIA NIM** (`NVIDIA_API_KEY`), **SambaNova** (`SAMBANOVA_API_KEY`), and **Mistral** (`MISTRAL_API_KEY`). These are optional — leave them blank if you do not want to use them. FreeClawRouter automatically skips providers without a key.

---

## 7. Start FreeClawRouter

In your Terminal or Command Prompt (inside the FreeClawRouter folder), run:

```
docker compose up -d --build
```

This builds the containers and starts everything in the background. The first time it runs it may take 2–3 minutes to download the container images.

To check if it started successfully:

```
docker compose logs freeclawrouter
```

You should see a startup message listing your active providers.

---

## 8. Open the dashboard

Once FreeClawRouter is running, open your web browser and go to:

**[http://localhost:8765/dashboard](http://localhost:8765/dashboard)**

You will see:

- **Usage tab** — live charts showing requests, tokens, and provider health
- **Messaging Apps tab** — connect Telegram or WhatsApp to your AI assistant
- **Settings tab** — choose whether to use your local AI model for simple tasks

The dashboard refreshes automatically every 10 seconds.

---

## 9. Connect messaging apps

To chat with your AI assistant through Telegram or WhatsApp, click the **Messaging Apps** tab in the dashboard. It has step-by-step instructions for each app with copy-paste commands and guidance on privacy settings.

---

## 10. Troubleshooting

**The dashboard is not loading**
- Make sure Docker Desktop is running (check for the whale icon)
- Run `docker compose up -d` again from the FreeClawRouter folder
- Check the logs with `docker compose logs freeclawrouter`

**"No API providers configured" warning in the logs**
- Open your `.env` file and make sure you have pasted at least one API key
- Save the file, then restart: `docker compose restart freeclawrouter`

**Ollama model not found / local AI not working**
- Make sure Ollama is running on your machine (open the Ollama app or run `ollama serve` in a terminal)
- Run `ollama pull gpt-oss:20b` if you haven't already
- On Linux, verify Docker can reach the host: `docker exec freeclawrouter_proxy curl -s http://host.docker.internal:11434` should return a response

**Everything is slow / using local AI all the time**
- Check the **Usage** tab in the dashboard — the provider health dots show the current status of each API service
- If all dots are red or yellow, your daily free quotas may be exhausted; they reset at midnight UTC

**I want to stop FreeClawRouter**
```
docker compose down
```

**I want to update to the latest version**
```
git pull
docker compose up -d --build
```
