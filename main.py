from google import genai
import os
from wordpress_xmlrpc import Client, WordPressPost
from wordpress_xmlrpc.methods.posts import NewPost
from wordpress_xmlrpc.methods.media import UploadFile
from wordpress_xmlrpc.compat import xmlrpc_client
from dotenv import load_dotenv
import tempfile
import base64
from PIL import Image
import io
import requests
from datetime import datetime
from diffusers import StableDiffusionPipeline
import torch

# Load environment variables
load_dotenv()

# WordPress configuration
WORDPRESS_URL = os.getenv('WORDPRESS_URL')
WORDPRESS_USERNAME = os.getenv('WORDPRESS_USERNAME')
WORDPRESS_PASSWORD = os.getenv('WORDPRESS_PASSWORD')

# API Keys
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
if not GOOGLE_API_KEY:
    raise ValueError("GOOGLE_API_KEY not found in .env file")

HUGGINGFACE_API_KEY = os.getenv('HUGGINGFACE_API_KEY')
if not HUGGINGFACE_API_KEY:
    raise ValueError("HUGGINGFACE_API_KEY not found in .env file")

# Initialize Google API client
client = genai.Client(api_key=GOOGLE_API_KEY)

# Initialize Stable Diffusion pipeline (this will download the model first time)
pipe = None

def get_pipeline():
    global pipe
    if pipe is None:
        # Initialize the pipeline with the small model
        pipe = StableDiffusionPipeline.from_pretrained(
            "CompVis/stable-diffusion-v1-4",
            torch_dtype=torch.float32,
            use_auth_token=HUGGINGFACE_API_KEY
        )
        if torch.cuda.is_available():
            pipe = pipe.to("cuda")
    return pipe

def extract_title_and_body(text):
    """Extract title and body from generated text."""
    title = ""
    body = ""

    # Split the text into lines
    lines = text.split('\n')
    
    # Find the title line
    for i, line in enumerate(lines):
        if line.startswith('Title:'):
            title = line[6:].strip()  # Remove "Title:" and whitespace
            # Body starts after the title line
            body = '\n'.join(lines[i+1:]).strip()
            break
        # Also check for a line that just looks like a title (for blog format)
        elif i == 0 and not line.startswith('Body:') and not line.startswith('Introduction:'):
            title = line.strip()
            # Body starts from the second line
            body = '\n'.join(lines[1:]).strip()
            break

    # If no title found using above methods, try the original method
    if not title:
        title_start = text.lower().find("title:")
        body_start = text.lower().find("body:")
        
        if title_start != -1 and body_start != -1 and body_start > title_start:
            title = text[title_start + len("title:"):body_start].strip()
            body = text[body_start + len("body:"):].strip()
        else:
            # Fallback: put everything in body
            body = text.strip()

    return title, body

def clean_body_text(text):
    """Clean and format the body text."""
    # Remove any "Body:" prefix if it exists
    if text.lstrip().startswith('Body:'):
        text = text[text.find('Body:') + 5:].lstrip()
    
    lines = text.split('\n')
    cleaned_lines = [line.strip() for line in lines if line.strip()]
    return '\n'.join(cleaned_lines)

def get_prompt_for_content_type(content_type, topic, keywords=None):
    """Get the appropriate prompt based on content type."""
    keyword_instruction = ""
    if keywords and keywords.strip():
        keyword_instruction = f"\nIMPORTANT: You MUST naturally incorporate the following keywords in the content: {keywords}"
    
    if content_type == "Case Study":
        return f"""generate a case study title and body for sfHawk. for the given prompt: {topic}
The case study body should be less than 250 words.{keyword_instruction}

Format your response as follows:

Title: <A short title under 20 words>

Body:
Problem Statement: A short, clear description of the core problem faced by manufacturers or users.

How sfHawk Helps: Explain step-by-step how the product addresses this issue, including features like automated monitoring, pre-alerts to different departments, CNC program transfer, and any other relevant functions. Present these points as a bulleted list using filled circular bullets (•).

Benefits: Summarize the tangible benefits achieved, such as reduced human dependency, improved process efficiency, better coordination between departments, reduced wastage of time, and improved inventory management. Present these benefits as a bulleted list using filled circular bullets (•).

Conclusion: Wrap up with key recommendations or final thoughts on the impact of the solution.
"""
    elif content_type == "Blog":
        return f"""Write a compelling blog article based on the topic: "{topic}"{keyword_instruction}

Format your response exactly as follows:

Title: A short, engaging headline under 20 words that captures the essence of the topic's impact on manufacturing.

Body:

Introduction: A concise, engaging opening paragraph that hooks the reader, introduces the topic, and highlights the importance of both technology and people in successful adoption.

Main Content: Explain in detail how the topic relates to manufacturing, focusing on real-world challenges like employee resistance, mindset change, and the need for training. Describe practical solutions (like sfHawk's) that help address these challenges, including examples of data-driven alerts, rewards and recognition programs, and effective change management strategies.

Key Points: Use bullet points to summarize 3-4 actionable insights or takeaways that highlight the importance of people in manufacturing transformation.

Conclusion: A strong closing statement that reinforces the message that technology alone is not enough. Encourage manufacturers to adopt a holistic approach and include a call to action inviting them to learn more from sfHawk.

IMPORTANT: Start your response with "Title:" followed by your title, then "Body:" before the main content.

Use a clear, professional tone, easy-to-read paragraphs, and ensure that the article is easy to read, engaging, and offers practical advice."""
    else:
        raise ValueError(f"Unsupported content type: {content_type}")

def generate_content(topic, content_type="Case Study", keywords=None):
    """Generate content using Gemini API based on content type."""
    try:
        prompt = get_prompt_for_content_type(content_type, topic, keywords)
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt
        )

        raw_text = response.text
        title, body = extract_title_and_body(raw_text)
        body = clean_body_text(body)

        # Remove markdown bold formatting for plain text display
        for md_bold in ["**Problem Statement:**", "**How sfHawk Helps:**", "**Benefits:**", "**Conclusion:**",
                       "**Introduction:**", "**Main Content:**", "**Key Points:**"]:
            body = body.replace(md_bold, md_bold.strip('*'))
        # Also remove any remaining ** if any
        body = body.replace("**", "")
        # Add newline before headings for readability
        for heading in ["Problem Statement:", "How sfHawk Helps:", "Benefits:", "Conclusion:",
                       "Introduction:", "Main Content:", "Key Points:"]:
            body = body.replace(heading, f"\n{heading}")

        return title, body

    except Exception as e:
        if "429" in str(e):
            return "Error: Quota exceeded. Please check your API plan and billing details, and try again later.", ""
        return f"Error generating content: {str(e)}", ""

def generate_image(prompt):
    """Generate an image using Hugging Face's API."""
    try:
        # API endpoint for a more reliable model
        API_URL = "https://api-inference.huggingface.co/models/stabilityai/stable-diffusion-xl-base-1.0"
        
        # Headers for the API request
        headers = {
            "Authorization": f"Bearer {HUGGINGFACE_API_KEY}"
        }
        
        # Body of the request - keeping it simple
        payload = {
            "inputs": prompt,
        }

        print(f"Making request to {API_URL}")  # Debug print
        
        # Make the API request
        response = requests.post(API_URL, headers=headers, json=payload)
        
        print(f"Response status: {response.status_code}")  # Debug print
        print(f"Response headers: {response.headers}")  # Debug print
        
        if response.status_code == 404:
            raise Exception("Model not found. Please check the model URL or try a different model.")
        
        if response.status_code != 200:
            # Try to get error message from response
            try:
                error_data = response.json()
                error_msg = error_data.get('error', 'Unknown error occurred')
            except:
                error_msg = response.text if response.text else f"HTTP Status {response.status_code}"
            raise Exception(f"API request failed: {error_msg}")
        
        # Check if we got image data
        content_type = response.headers.get('content-type', '')
        if not content_type.startswith('image/'):
            raise Exception(f"Expected image response, got {content_type}")
            
        # Get the image data from the response
        image_bytes = response.content
        
        # Create a PIL Image object
        image = Image.open(io.BytesIO(image_bytes))
        
        # Save to a temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as tmp_file:
            image.save(tmp_file, format='PNG')
            return tmp_file.name

    except requests.exceptions.RequestException as e:
        raise Exception(f"Network error: {str(e)}")
    except Exception as e:
        raise Exception(f"Error generating image: {str(e)}")

def upload_to_wordpress(title, body, images=None):
    """Upload content to WordPress with optional images."""
    if not all([WORDPRESS_URL, WORDPRESS_USERNAME, WORDPRESS_PASSWORD]):
        raise ValueError("WordPress credentials not configured properly")
    
    try:
        # Initialize WordPress client
        wp = Client(WORDPRESS_URL, WORDPRESS_USERNAME, WORDPRESS_PASSWORD)
        
        # Create new post
        post = WordPressPost()
        post.title = title
        post.post_status = 'publish'
        
        # Format today's date
        today = datetime.now()
        day = str(today.day)
        formatted_date = f"{day} - {today.strftime('%B')} - {today.strftime('%Y')}"
        
        # Start building content with proper HTML structure
        content = f'''
<div class="post-container">
    <p class="post-date" style="font-style: italic; margin: 20px 0;">{formatted_date}</p>
'''
        
        # Upload and add images if provided
        if images:
            content += '<div class="post-images">\n'
            for idx, image_data in enumerate(images):
                if image_data:
                    tmp_file = None
                    try:
                        # Create temporary file for the image
                        tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.png')
                        tmp_file.write(image_data)
                        tmp_file.close()  # Close the file before reading it again
                        
                        # Prepare file data
                        data = {
                            'name': f'image_{idx + 1}.png',
                            'type': 'image/png',
                        }
                        
                        # Read the temporary file
                        with open(tmp_file.name, 'rb') as img:
                            data['bits'] = xmlrpc_client.Binary(img.read())
                        
                        # Upload to WordPress
                        response = wp.call(UploadFile(data))
                        if 'url' in response:
                            content += f'    <img src="{response["url"]}" alt="Content Image {idx + 1}" style="margin: 10px 0;" />\n'
                    
                    except Exception as e:
                        raise Exception(f"Error uploading image {idx + 1}: {str(e)}")
                    
                    finally:
                        # Clean up temporary file
                        if tmp_file and os.path.exists(tmp_file.name):
                            try:
                                os.unlink(tmp_file.name)
                            except Exception:
                                pass  # Ignore cleanup errors
            
            content += '</div>\n'
        
        # Add the body content with proper formatting
        content += f'''
<div class="post-body">
    {body}
</div>
</div>'''
        
        # Set the complete content
        post.content = content
        
        # Publish post
        post_id = wp.call(NewPost(post))
        
        if not post_id:
            raise Exception("Failed to get post ID after publishing")
            
        return post_id
        
    except Exception as e:
        raise Exception(f"Error publishing to WordPress: {str(e)}") 
