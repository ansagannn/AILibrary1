"""Create local operator accounts while the application is stopped."""
import argparse,getpass,sqlite3,sys
from app.main import conn, init
from app import auth

def main():
 p=argparse.ArgumentParser();p.add_argument('username');p.add_argument('--role',required=True,choices=['admin','librarian']);a=p.parse_args()
 one=getpass.getpass('Пароль (минимум 12 символов): '); two=getpass.getpass('Повторите пароль: ')
 if one!=two: print('Пароли не совпадают',file=sys.stderr);return 1
 try:
  with conn() as c:
   auth.create_user(c,a.username,one,a.role)
   auth.audit(c,'local-cli','create_user',a.username,'role='+a.role)
  print('Учётная запись создана');return 0
 except (ValueError,sqlite3.Error) as e: print('Ошибка:',e,file=sys.stderr);return 1
if __name__=='__main__':sys.exit(main())
