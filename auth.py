"""Minimal local authentication; requires TLS and further security review for deployment."""
import hashlib, hmac, secrets, time

ROLES={'admin','librarian'}

def init(c):
    c.execute('''CREATE TABLE IF NOT EXISTS qor_users (
      id INTEGER PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE UNIQUE,
      password_salt BLOB NOT NULL, password_hash BLOB NOT NULL,
      role TEXT NOT NULL CHECK(role IN ('admin','librarian')), active INTEGER NOT NULL DEFAULT 1)''')
    c.execute('''CREATE TABLE IF NOT EXISTS qor_sessions (
      token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES qor_users(id), expires_at INTEGER NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS qor_audit (
      id INTEGER PRIMARY KEY, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', details TEXT NOT NULL DEFAULT '')''')

def password_hash(password, salt):
    return hashlib.scrypt(password.encode('utf-8'),salt=salt,n=2**14,r=8,p=1,dklen=32)

def create_user(c, username, password, role):
    username=username.strip()
    if not 3 <= len(username) <= 100 or not 12 <= len(password) <= 256 or role not in ROLES:
        raise ValueError('Имя от 3 до 100 символов, пароль от 12 до 256 символов, роль admin или librarian')
    salt=secrets.token_bytes(16)
    c.execute('INSERT INTO qor_users(username,password_salt,password_hash,role) VALUES(?,?,?,?)',
              (username,salt,password_hash(password,salt),role))

def login(c, username, password):
    row=c.execute('SELECT id,username,password_salt,password_hash,role,active FROM qor_users WHERE username=?', (username.strip(),)).fetchone()
    salt=row['password_salt'] if row else b'\0'*16
    expected=row['password_hash'] if row else b'\0'*32
    computed=password_hash(password[:256],salt)
    if not hmac.compare_digest(computed,expected) or not row or not row['active']: return None
    token=secrets.token_urlsafe(32); digest=hashlib.sha256(token.encode()).hexdigest()
    c.execute('INSERT INTO qor_sessions(token_hash,user_id,expires_at) VALUES(?,?,?)',(digest,row['id'],int(time.time())+8*3600))
    return token, {'id':row['id'],'username':row['username'],'role':row['role']}

def lookup(c, token):
    if not token or len(token)>200: return None
    digest=hashlib.sha256(token.encode()).hexdigest()
    row=c.execute('''SELECT u.id,u.username,u.role FROM qor_sessions s JOIN qor_users u ON u.id=s.user_id
                      WHERE s.token_hash=? AND s.expires_at>? AND u.active=1''',(digest,int(time.time()))).fetchone()
    return dict(row) if row else None

def logout(c, token):
    c.execute('DELETE FROM qor_sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))

def audit(c, actor, action, target='', details=''):
    c.execute('INSERT INTO qor_audit(actor,action,target,details) VALUES(?,?,?,?)',(actor,action,str(target),details))
