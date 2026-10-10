import mysql.connector
from config import DB_CONFIG

def conn(): return mysql.connector.connect(**DB_CONFIG)
def one(q,p=()):
    c=conn(); cur=c.cursor(dictionary=True)
    try: cur.execute(q,p); return cur.fetchone()
    finally: cur.close(); c.close()
def all_(q,p=()):
    c=conn(); cur=c.cursor(dictionary=True)
    try: cur.execute(q,p); return cur.fetchall()
    finally: cur.close(); c.close()
def run(q,p=()):
    c=conn(); cur=c.cursor()
    try: cur.execute(q,p); c.commit(); return cur.lastrowid
    finally: cur.close(); c.close()
