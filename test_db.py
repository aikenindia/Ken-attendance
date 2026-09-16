import mysql.connector

conn = mysql.connector.connect(
    host="localhost",
    port=3306,
    database="face_attendance",
    user="root",
    password=""    # <-- your MySQL password
)

cur = conn.cursor()
cur.execute("SHOW TABLES;")
print("Tables:", cur.fetchall())
conn.close()
print("SUCCESS — connected!")