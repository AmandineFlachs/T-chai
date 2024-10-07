import re
import string
import argparse
import streamlit as st
import urllib.request
import urllib.error
import http.client
from bs4 import BeautifulSoup
from PIL import Image

from ibm_watsonx_ai import Credentials, APIClient
from ibm_watsonx_ai.foundation_models import ModelInference
from ibm_watsonx_ai.foundation_models.utils.enums import ModelTypes, PromptTemplateFormats
from ibm_watsonx_ai.foundation_models.prompts import PromptTemplateManager
from ibm_watsonx_ai.metanames import GenTextParamsMetaNames as GenParams
from ibm_watson import TextToSpeechV1
from ibm_cloud_sdk_core.authenticators import IAMAuthenticator

from openai import OpenAI

parser = argparse.ArgumentParser(description="T-chai UI")
parser.add_argument("--use_local_inference", action="store_true", help="Use local inference", required=False)
parser.add_argument("--watsonxai_project_id", type=str, help="watsonx.ai project id", required=True)
parser.add_argument("--watsonxai_api_key", type=str, help="watsonx.ai API key", required=True)
parser.add_argument("--watson_iam_auth", type=str, help="Watson IAM", required=True)
args = parser.parse_args()

use_local_inference = args.use_local_inference
watsonxai_project_id = args.watsonxai_project_id
watsonxai_api_key = args.watsonxai_api_key
watson_iam_auth = args.watson_iam_auth

logo_path = "./assets/t-chai_logo.png"

# Header

st.set_page_config(layout="wide", page_title="T-chai", page_icon=Image.open(logo_path))

# Sidebar/Options

st.sidebar.image(logo_path, width=150)
st.sidebar.header("Profile")
st.sidebar.write("Please fill your profile to get customized answers from T-chai. You can then ask questions about your children educational curriculum or let them ask T-chai to help with their homework and learning journey.")

mode = st.sidebar.selectbox("Mode:", ["I am a student", "I am a parent"])
age = st.sidebar.selectbox("Student age:", ["5-10 years", "11-13 years", "14-18 years", "19+ years"])

st.sidebar.divider()
st.sidebar.write("Advanced options:")
use_rag = st.sidebar.checkbox("Use RAG [Wikipedia]")

# Main

st.title("T-chai")

def get_prompt_template(st, mode):
    """"Get prompt template for mode using manager stored in st."""

    templates = st.session_state.prompt_manager.list()
    template_id = templates.loc[templates["NAME"] == mode]["ID"].values[0]
    template = st.session_state.prompt_manager.load_prompt(template_id, PromptTemplateFormats.STRING)
    template = template.replace("<|system|>", "").replace("<|assistant|>", "").strip()

    return template

def get_system_prompt(st, mode, age):
    """Get system prompt for mode and age."""

    assert mode.startswith("I am a ")

    return st.session_state.prompt_templates[mode.replace("I am a ", "", 1)].format(age=age)

def reset_messages(st, mode, age):
    """Reset list of messages to default for mode and age."""

    st.session_state.messages = []
    st.session_state.messages.append({"role": "system", "content": get_system_prompt(st, mode, age)})

def diplay_help_message(st, mode):
    """Display help message for mode."""

    student_example = "For example: Can you help me understand the difference between a planet and a dwarf planet?"
    parent_example = "For example: How can I help my child learn planets in the solar system?"

    if mode == "I am a student":
        prompt_example = student_example
    else:
        prompt_example = parent_example

    help_message = {
        "role": "assistant",
        "content": "Questions and answers will appear here. Please type your questions below. " + prompt_example
    }

    with st.chat_message(help_message["role"]):
        st.markdown(help_message["content"])

def read_aloud(st, message):
    """Add Read aloud button and, when clicked, trigger text to speech via Watson Tech To Speech, then an audio player for the reply."""

    reset = st.button("Read aloud")

    if reset:
        authenticator = IAMAuthenticator(watson_iam_auth)
        text_to_speech = TextToSpeechV1(authenticator=authenticator)

        text_to_speech.set_service_url("https://api.eu-gb.text-to-speech.watson.cloud.ibm.com")
        with open("audio.mp3", "wb") as f:
            f.write(text_to_speech.synthesize(message, voice="en-US_LisaV3Voice", accept="audio/mp3").get_result().content)

        st.audio("audio.mp3", format="audio/mp3")

def get_information_from_wikipedia(keyword, max_len):
    """Retrieve information from Wikipedia page corresponding to keyword. If invalid, return an empty string."""

    url = f"https://en.wikipedia.org/wiki/{keyword}"

    try:
        source = urllib.request.urlopen(url).read()
    except (urllib.error.HTTPError, http.client.InvalidURL):
        return ""

    soup = BeautifulSoup(source, "lxml")

    # Post-process retrieved HTML page.
    info = [str(p.text) for p in soup.find_all("p")]
    info = "".join(info)
    info = info.replace(u"\n", u"") # Remove \n.
    info = info.replace(u"\xa0", u"") # Remove \xa0.
    info = re.sub(u"\[.*?\]", "", info) # Remove references, e.g. [123].
    info = info[:max_len]

    return info

def get_rag_keyword(rag_response):
    """
    Post-process RAG response to extract keyword that will be used to query Wikipedia.
    Return an empty string if input is invalid, which automatically disables RAG.
    """
    
    def get_rag_keyword_candidate(rag_response):
        rag_response = rag_response.strip()

        # Single word response.
        if len(rag_response.split(" ")) == 1:
            return rag_response

        try:
            # Response is between double-quotes (first occurence only).
            return rag_response.split("\"")[1]
        except:
            # No candidate.
            return ""

    rag_response = get_rag_keyword_candidate(rag_response)

    # Candidate post-processing (remove punctuation, multi-word reponse).
    rag_response = rag_response.translate(str.maketrans("", "", string.punctuation))

    # Insert underscore if multiple words into a single word, such as "SolarSystem".
    rag_response = re.sub(r"(?<=\w)([A-Z])", r"_\1", rag_response)

    # Replace spaces with underscores.
    rag_response = rag_response.replace(" ", "_")

    return rag_response

def inference(use_local_inference, messages):
    """
    Run inference with messages as input, using IBM Granite via a local vllm if
    use_local_inference or watsonx.ai Foundation Models otherwise.
    """

    if use_local_inference:
        return st.session_state.client.chat.completions.create(
            model="ibm-granite/granite-7b-instruct",
            messages=messages,
            max_tokens=512
        ).choices[0].message.content
    else:
        # Apply chat template to messages (history and new prompt).
        prompt = ""
        for message in messages:
            prompt += "<|" + message["role"] + "|>" + "\n" + message["content"] + "\n"
        prompt += "<|assistant|>\n"

        return st.session_state.model.generate_text(prompt, guardrails=True)

if "mode" in st.session_state and (st.session_state.mode != mode or st.session_state.age != age):
    reset_messages(st, mode, age)

st.session_state.mode = mode
st.session_state.age = age

if "messages" not in st.session_state:
    credentials = Credentials(url="https://eu-gb.ml.cloud.ibm.com", api_key=watsonxai_api_key)
    client = APIClient(credentials)
    client.set.default_project(watsonxai_project_id)

    if use_local_inference: # Inference via local vLLM.
        st.session_state.client = OpenAI(base_url="http://localhost:8000/v1", api_key="EMPTY")
    else: # Inference via IBM watsonx.ai Foundation Models.
        parameters = {
            GenParams.DECODING_METHOD: "greedy",
            GenParams.MAX_NEW_TOKENS: 512,
        }

        st.session_state.model = ModelInference(
            model_id=ModelTypes.GRANITE_13B_CHAT_V2,
            params=parameters, 
            credentials=credentials,
            project_id=watsonxai_project_id)

    st.session_state.prompt_manager = PromptTemplateManager(
        credentials=credentials,
        project_id=watsonxai_project_id)

    st.session_state.prompt_templates = {mode : get_prompt_template(st, mode) for mode in ["student", "parent"]}

    reset_messages(st, mode, age)

diplay_help_message(st, mode)

for message in st.session_state.messages:
    if message["role"] != "system":
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

if prompt := st.chat_input("Ask me anything..."):
    st.session_state.messages.append({"role": "user", "content": prompt})

    with st.chat_message("user"):
        st.markdown(prompt)

    with st.spinner(text="Thinking..."):
        if use_rag:
            rag_message = [{
                "role": "user",
                "content": f"I want to find the best wikipedia page to answer this question: \"{prompt}\". Reply a single word."
            }]

            rag_response = inference(use_local_inference, rag_message)

            rag_keyword = get_rag_keyword(rag_response)

            if rag_keyword:
                info = get_information_from_wikipedia(rag_keyword, max_len=1024)
                augmented_prompt = info + "\n" + prompt

                # Patch original prompt with augmented version.
                st.session_state.messages[-1] = {"role": "user", "content": augmented_prompt}

                status = "SUCCESS" if info else "FAILED"
                rag_chat_message = f"[with RAG keyword: '{rag_keyword}', retrieval status: {status}]"

                with st.chat_message("INFO"):
                    st.markdown(rag_chat_message)
            else:
                rag_chat_message = None

        response = inference(use_local_inference, st.session_state.messages)

        if use_rag and rag_chat_message:
            # Restore original prompt.
            st.session_state.messages[-1] = {"role": "user", "content": prompt}
            st.session_state.messages.append({"role": "info", "content": rag_chat_message})

    st.session_state.messages.append({"role": "assistant", "content": response})

    with st.chat_message("assistant"):
        st.markdown(response)

if len(st.session_state.messages) > 1:
    read_aloud(st, message=st.session_state.messages[-1]["content"])

    reset = st.button("Start new conversation")

    if reset:
        reset_messages(st, mode, age)
        st.rerun()

# Footer

hide_default_format = """
       <style>
       #MainMenu {visibility: hidden; }
       footer {visibility: hidden;}
       </style>
       """
st.markdown(hide_default_format, unsafe_allow_html=True)
