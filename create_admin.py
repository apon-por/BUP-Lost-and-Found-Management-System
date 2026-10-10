from werkzeug.security import generate_password_hash
from database import one,run
email='admin@bup.edu.bd'; password='Admin@123'
if one('SELECT user_id FROM users WHERE email=%s',(email,)): print('Admin already exists')
else:
 run('INSERT INTO users(name,email,password,role,department) VALUES(%s,%s,%s,%s,%s)',('System Administrator',email,generate_password_hash(password),'admin','ICT'))
 print('Created:',email,password)
