import os
from dotenv import load_dotenv
load_dotenv()
DB_CONFIG={'host':os.getenv('DB_HOST','127.0.0.1'),'port':int(os.getenv('DB_PORT','3306')),'database':os.getenv('DB_NAME','bup_lost_found'),'user':os.getenv('DB_USER','root'),'password':os.getenv('DB_PASSWORD','')}
JWT_SECRET=os.getenv('JWT_SECRET','change_this_secret')
UPLOAD_FOLDER=os.getenv('UPLOAD_FOLDER','uploads')
