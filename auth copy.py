from functools import wraps
from flask import request,jsonify
import jwt
from config import JWT_SECRET
from database import one

def token(u): return jwt.encode({'user_id':u['user_id'],'role':u['role'],'email':u['email']},JWT_SECRET,algorithm='HS256')
def user():
 h=request.headers.get('Authorization','')
 if not h.startswith('Bearer '): return None
 try:
  p=jwt.decode(h[7:],JWT_SECRET,algorithms=['HS256']); return one('SELECT user_id,name,email,role,phone,department,created_at FROM users WHERE user_id=%s',(p['user_id'],))
 except Exception:return None
def login_required(f):
 @wraps(f)
 def w(*a,**k):
  u=user()
  if not u:return jsonify(error='Authentication required'),401
  return f(u,*a,**k)
 return w
def admin_required(f):
 @wraps(f)
 def w(u,*a,**k):
  if u['role']!='admin':return jsonify(error='Administrator access required'),403
  return f(u,*a,**k)
 return w
