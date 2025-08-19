import requests
import json
import os
from dotenv import load_dotenv # Import load_dotenv

# Load environment variables from .env file
load_dotenv()

# --- Configuration ---
# Get API key from environment variables
# For security, the API key should be stored in a .env file and not hardcoded.
API_KEY = os.getenv("GEMINI_API_KEY") # Use os.getenv to retrieve the API key

# The Gemini API endpoint for generating content
API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-05-20:generateContent"

def get_gemini_general_response(prompt: str) -> str:
    """
    Sends a prompt to the Gemini API and returns a general, unstructured text response.

    Args:
        prompt (str): The user's input prompt.

    Returns:
        str: The generated text response from the Gemini model, or an error message.
    """
    # The payload is simplified to request only a general text response
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}]
    }

    headers = {
        "Content-Type": "application/json"
    }

    print("Sending request for general response to Gemini API...")
    try:
        # Check if API_KEY is set
        if not API_KEY:
            raise ValueError("GEMINI_API_KEY not found in environment variables. Please set it in a .env file.")

        response = requests.post(f"{API_URL}?key={API_KEY}", headers=headers, json=payload)
        response.raise_for_status() # Raise an exception for HTTP errors

        result = response.json()

        # Extract the general text response directly
        if result.get("candidates") and result["candidates"][0].get("content") and \
           result["candidates"][0]["content"].get("parts") and \
           result["candidates"][0]["content"]["parts"][0].get("text"):
            
            general_text_response = result["candidates"][0]["content"]["parts"][0]["text"]
            return general_text_response
        else:
            return f"Error: Unexpected response structure: {json.dumps(result, indent=2)}"

    except requests.exceptions.RequestException as e:
        return f"Error connecting to Gemini API: {e}"
    except json.JSONDecodeError:
        return f"Error decoding JSON response: {response.text}"
    except ValueError as e: # Catch the specific ValueError for missing API key
        return str(e)
    except Exception as e:
        return f"An unexpected error occurred: {e}"
    

    # --- Adapters for the app's router (append to aiapi.py) ---

def text_reply(prompt: str) -> str:
    """
    Single-turn adapter used by the router when no chat history is needed.
    """
    return get_gemini_general_response(prompt)

def chat_reply(session_id: str, history: list[tuple[str, str]], user_message: str) -> str:
    """
    Chat-style adapter: stitches a short context from history and calls Gemini.
    """
    context_lines = []
    for role, text in (history or [])[-8:]:  # last 8 turns
        who = "User" if role == "user" else "Assistant"
        context_lines.append(f"{who}: {text}")
    context = "\n".join(context_lines)
    prompt = f"{context}\nUser: {user_message}\nAssistant:"

    return get_gemini_general_response(prompt)


# --- Example Usage ---
if __name__ == "__main__":
    print("Welcome to the Gemini Chat App! Type 'exit' to quit.")
    print("Now, I'll provide general responses without specific structured fields.")
    print("Try asking: 'what is better, mercedes or bmw?' or 'tell me about the history of computers'")


    while True:
        user_input = input("You: ")
        if user_input.lower() == 'exit':
            print("Goodbye!")
            break

        response_text = get_gemini_general_response(user_input)

        if response_text.startswith("Error:"): # Check if the response is an error message
            print(f"Error: {response_text}")
        else:
            print("\n--- Gemini's Response ---")
            print(response_text)
            print("-------------------------\n")

