import sys
import os

# Ensure the root directory is in sys.path so 'crawler' and 'web' can be imported by Vercel
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from web.app import app

# Vercel looks for 'app' as the ASGI application entry point
