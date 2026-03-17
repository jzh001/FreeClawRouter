# Product Requirements Document: FreeClaw

## 1. Project Overview & Objective
FreeClaw is an open-source layer designed to run OpenClaw at zero API cost. It acts as an intelligent model router that dynamically leverages free-tier API endpoints (e.g., NVIDIA Build, OpenRouter) and a local LLM fallback. 

**CRITICAL INSTRUCTION - UNDERSTAND OPENCLAW FIRST:** Before starting this project, planning the architecture, or writing any code, you MUST actively search the web to fully understand what **OpenClaw** is and what it does. Research its repository, core functionalities, agentic behaviors, memory management, tool-calling schemas, and exactly how it communicates with standard LLM endpoints. Your implementation of FreeClaw must account for OpenClaw's exact operational behavior.

**CRITICAL INSTRUCTION - REVERSE PROXY:** FreeClaw must be built as a standalone **OpenAI-compatible reverse proxy**. You must NOT modify OpenClaw's core source code. OpenClaw will simply be configured to point its API base URL to `http://localhost:<FreeClaw-Port>`, and FreeClaw will handle the rest.

## 2. Core Architecture & Routing Engine
FreeClaw intercepts standard API payloads from OpenClaw and determines the best free endpoint to forward them to. 

### 2.1 Config-Driven API & Local Model Management
* Create a centralized configuration system (`config.yaml` or `.env` based).
* **CRITICAL AI CODER INSTRUCTION - WEB SEARCH REQUIRED:** Before generating the default configuration file, you MUST search the web to compile an up-to-date, comprehensive list of LLM platforms currently offering free API tiers (e.g., OpenRouter, NVIDIA Build, Groq, Together AI, Mistral, Cohere, etc.). You must extract their *exact* current free limits (Requests Per Minute, Tokens Per Minute, Requests Per Day) and context window sizes to populate the default configurations. 
* **External APIs:** Define available APIs, models, API keys, context window limits, and rate limits based on your search results. 
  * **Example Configuration:** For NVIDIA Build (NIM), the config should be able to map models like **Nemotron Super 120B (`nvidia/nemotron-3-super-120b-a12b`)** and enforce its specific free-tier rate limit of **40 RPM (Requests Per Minute)** and context limits.
* **Local Model Configuration:** The local LLM used for routing and fallback must be easily configurable here (e.g., defining the Ollama host URL and the model tag). It should default to `qwen:3.5:9b`, but users can seamlessly swap this to Llama 3, Mistral, or any other local model their hardware supports.

### 2.2 The Hybrid Router Pipeline
To minimize latency and compute overhead, the routing decision must be a two-step process:
1.  **Tier 1: Fast Heuristics:** Filter available models based on current rate limit exhaustion and context window constraints (e.g., calculating input tokens).
2.  **Tier 2: Local LLM Evaluation (Ollama):** If multiple free APIs are available, pass a lightweight observation of the request and remaining limits to the configured local LLM. The local LLM acts as the ultimate decision-maker based on request complexity.
3.  **Local Fallback:** If all free-tier API limits are exhausted, route the actual generation task to the configured local LLM. Display a visible console warning when this happens so the user knows they are running on local compute.

### 2.3 Agentic Routing Constraints (No Mid-Task Swaps)
OpenClaw relies on strict tool-calling schemas. To prevent parser crashes, do not swap models mid-task or mid-JSON-generation based on a rigid "time slice." Model routing should occur at the start of a new request or when a complete tool execution loop finishes. 

### 2.4 Context Manager
OpenClaw prompts (with memory, identity files, and workspace context) can easily exceed the context windows of free-tier APIs (e.g., 8K limits). Implement a Context Manager in FreeClaw that:
* Identifies the token limit of the chosen endpoint.
* Gracefully truncates the oldest conversation history or applies summarization safeguards to fit the prompt into the chosen free model's context window.

## 3. Hardware Constraints & Performance
* **Primary Hardware:** Mac Mini M4 Pro 24GB Unified Memory.
* **Compatibility:** Must also be fully compatible with NVIDIA GPUs.
* **OOM Safeguards:** The local LLM and proxy must run safely within the 24GB memory limit. Implement robust error management to catch Out of Memory (OOM) errors. If memory thresholds are approached during local fallback, trigger aggressive context summarization or halt cleanly.

## 4. Security & Sandboxing (Strict Enforcements)
1.  **Zero Key Leakage:** Under no circumstances should API keys from the config file be included in the payloads sent to the local LLM router for evaluation.
2.  **Docker Isolation:** Provide a `docker-compose.yml` that networks FreeClaw and OpenClaw together. OpenClaw is an autonomous agent; its container must have strictly limited access and zero volume-mount access to the host machine's root filesystem. 
3.  **Testing Environment:** All test executions written by the AI must occur strictly within the Docker container. **Do NOT execute tests in the host local shell.**

## 5. Documentation Requirements
Create the following documentation:
1.  `README.md`: User-facing deployment instructions. Must be simple, explaining how to set up Ollama, adjust the local model config, populate the config file with free API keys, and launch the Docker Compose stack.
2.  `ARCHITECTURE.md`: Technical documentation explaining how the reverse proxy abstraction works, how the Context Manager modifies payloads, and how the Hybrid Router makes decisions without altering OpenClaw.