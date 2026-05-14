"""LLM client protocol + adapters.

aitester-bdd is provider-agnostic via the LLMClient Protocol. The default
implementation delegates to robotframework-aiagent, which supports
OpenAI, Anthropic, Gemini, Vertex AI, Mistral, Groq, Cohere, Bedrock,
and Hugging Face out of the box.
"""
