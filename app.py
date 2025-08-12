"""
Smart Learning App — Full (Phase A+B+C merged)
File: smart_learning_app_full.py

Run:
    pip install streamlit pandas ics
    streamlit run smart_learning_app_full.py
"""

import streamlit as st
import sqlite3
from datetime import date, datetime, timedelta, time
import pandas as pd
import math
import io
import os
from ics import Calendar, Event

DB = "smart_learning_full.db"

# ----------------- Utilities -----------------
def safe_rerun():
    try:
        st.rerun()
    except Exception:
        try:
            st.experimental_rerun()
        except Exception:
            pass

def db_connect():
    return sqlite3.connect(DB)

# ----------------- DB Init -----------------
def init_db(seed_demo=True):
    new_db = not os.path.exists(DB)
    conn = db_connect()
    c = conn.cursor()

    c.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY,
            subject TEXT,
            topic TEXT,
            minutes INTEGER,
            difficulty INTEGER,
            priority INTEGER,
            deadline TEXT,
            scheduled_date TEXT,
            time_window TEXT, -- morning/afternoon/evening/any
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY,
            task_id INTEGER,
            duration_minutes INTEGER,
            timestamp TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS quizzes (
            id INTEGER PRIMARY KEY,
            topic TEXT,
            question TEXT,
            answer_text TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS progress (
            topic TEXT PRIMARY KEY,
            completed INTEGER DEFAULT 0,
            last_score INTEGER DEFAULT 0
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY,
            original_task_id INTEGER,
            review_task_id INTEGER,
            interval_days INTEGER
        )
    ''')
    conn.commit()
    conn.close()

    if new_db and seed_demo:
        seed_demo_data()

# ----------------- Seed demo data -----------------
def seed_demo_data():
    today = date.today()
    add_task('Mathematics','Calculus - Limits & Continuity', 90, 4, 5, today + timedelta(days=10), time_window='morning')
    add_task('Mathematics','Linear Algebra - Matrix Multiplication', 60, 3, 4, today + timedelta(days=14))
    add_task('Python','Generators and Iterators', 45, 3, 4, today + timedelta(days=7), time_window='evening')
    add_task('Algorithms','Greedy Algorithms - Problems', 120, 5, 5, today + timedelta(days=20))
    add_task('History','World War II - Overview', 40, 2, 2, today + timedelta(days=30))
    # sample quiz questions
    add_quiz('Generators and Iterators','What keyword defines a generator function?','def')
    add_quiz('Calculus - Limits & Continuity','Limit of sin(x)/x as x->0 equals?','1')

# ----------------- DB helpers -----------------
def run_query(q, params=(), fetch=False):
    conn = db_connect()
    c = conn.cursor()
    c.execute(q, params)
    if fetch:
        rows = c.fetchall()
        conn.commit()
        conn.close()
        return rows
    conn.commit()
    conn.close()

def add_task(subject, topic, minutes, difficulty, priority, deadline, status='pending', time_window='any'):
    if isinstance(deadline, (date, datetime)):
        dl = deadline.isoformat()
    else:
        dl = str(deadline)
    run_query('INSERT INTO tasks (subject, topic, minutes, difficulty, priority, deadline, status, time_window) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
              (subject, topic, minutes, difficulty, priority, dl, status, time_window))

def fetch_tasks(where_clause="", params=()):
    q = 'SELECT id,subject,topic,minutes,difficulty,priority,deadline,scheduled_date,time_window,status,created_at FROM tasks'
    if where_clause:
        q += ' WHERE ' + where_clause
    q += ' ORDER BY scheduled_date IS NULL, scheduled_date, deadline'
    rows = run_query(q, params, fetch=True)
    cols = ["id","subject","topic","minutes","difficulty","priority","deadline","scheduled_date","time_window","status","created_at"]
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows, columns=cols)

def update_task_schedule(task_id, scheduled_dt, time_window='any'):
    run_query('UPDATE tasks SET scheduled_date = ?, status = ?, time_window = ? WHERE id = ?', (scheduled_dt.isoformat(), 'pending', time_window, task_id))

def update_task_status(task_id, status):
    run_query('UPDATE tasks SET status = ? WHERE id = ?', (status, task_id))

def record_session(task_id, duration_minutes):
    run_query('INSERT INTO sessions (task_id, duration_minutes, timestamp) VALUES (?, ?, ?)', (task_id, duration_minutes, datetime.now().isoformat()))
    # adjust estimated minutes per-subject learning: simple moving average multiplier
    # We'll compute multiplier when scheduling

def add_quiz(topic, question, answer):
    run_query('INSERT INTO quizzes (topic, question, answer_text) VALUES (?, ?, ?)', (topic, question, answer))

def fetch_quizzes_for_topic(topic):
    rows = run_query('SELECT id,topic,question,answer_text FROM quizzes WHERE topic = ?', (topic,), fetch=True)
    cols = ['id','topic','question','answer_text']
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows, columns=cols)

def mark_topic_completed(topic, score):
    found = run_query('SELECT topic FROM progress WHERE topic = ?', (topic,), fetch=True)
    if found:
        run_query('UPDATE progress SET completed = 1, last_score = ? WHERE topic = ?', (score, topic))
    else:
        run_query('INSERT INTO progress (topic, completed, last_score) VALUES (?, ?, ?)', (topic,1,score))

def get_progress():
    rows = run_query('SELECT topic,completed,last_score FROM progress', fetch=True)
    return {r[0]: {'completed': r[1], 'last_score': r[2]} for r in rows}

# ----------------- Scheduling algorithm -----------------
def smart_schedule(daily_minutes, review_intervals=[1,3,7], lookahead_days=60, time_preferences=None):
    """
    - schedules unscheduled pending tasks into days up to daily_minutes
    - prioritizes deadline -> priority -> difficulty
    - tries to respect time_window and time_preferences where possible
    - creates review tasks for first scheduled occurrence
    """
    if time_preferences is None:
        time_preferences = {'morning':1.0,'afternoon':1.0,'evening':1.0,'any':1.0}

    today = date.today()
    pending = fetch_tasks("status = 'pending' AND scheduled_date IS NULL")
    if pending.empty:
        return 0

    # read sessions to compute subject-speed multipliers
    speed = compute_subject_speed()  # dict subject -> multiplier (<=1 faster, >1 slower)
    pending['deadline_date'] = pd.to_datetime(pending['deadline']).dt.date
    pending = pending.sort_values(by=['deadline_date','priority','difficulty'], ascending=[True, False, False])

    schedule_days = [today + timedelta(days=i) for i in range(lookahead_days)]
    day_capacity = {d.isoformat(): daily_minutes for d in schedule_days}
    scheduled_count = 0
    first_scheduled_for_topic = {}

    for _, row in pending.iterrows():
        tid = int(row['id'])
        original_minutes = int(row['minutes'])
        subject = row['subject']
        minutes_needed = max(5, int(original_minutes * speed.get(subject, 1.0)))
        preferred_window = row['time_window'] or 'any'
        deadline = row['deadline_date']

        # candidates: prefer before deadline
        candidates = [d for d in schedule_days if d <= deadline] or schedule_days

        placed = False
        # try prefer days matching time window and user's preference
        # we'll attempt candidates in order but prefer days where time preference weight is higher
        for d in candidates:
            key = d.isoformat()
            # check capacity and (optionally) window match
            if day_capacity[key] >= minutes_needed:
                # simple time_window handling: prefer days that are available; we don't schedule time-of-day slots strictly here
                update_task_schedule(tid, datetime.combine(d, datetime.min.time()), time_window=preferred_window)
                day_capacity[key] -= minutes_needed
                scheduled_count += 1
                if subject not in first_scheduled_for_topic:
                    first_scheduled_for_topic[subject] = d
                placed = True
                break

        if not placed:
            # try full lookahead without deadline restriction
            for d in schedule_days:
                key = d.isoformat()
                if day_capacity[key] >= minutes_needed:
                    update_task_schedule(tid, datetime.combine(d, datetime.min.time()), time_window=preferred_window)
                    day_capacity[key] -= minutes_needed
                    scheduled_count += 1
                    if subject not in first_scheduled_for_topic:
                        first_scheduled_for_topic[subject] = d
                    placed = True
                    break
        # if still not placed, leave unscheduled

    # create review tasks
    for subject, first_date in first_scheduled_for_topic.items():
        # find first task for subject
        rows = run_query('SELECT id,topic,minutes FROM tasks WHERE subject = ? ORDER BY created_at LIMIT 1', (subject,), fetch=True)
        if not rows:
            continue
        original_id, topic, minutes = rows[0]
        for offset in review_intervals:
            review_date = first_date + timedelta(days=offset)
            if review_date <= today + timedelta(days=lookahead_days):
                review_minutes = max(10, math.ceil(minutes * 0.25))
                add_task(subject + ' (review)', topic + ' - review', review_minutes, 1, 3, review_date, status='review')
                last = run_query('SELECT last_insert_rowid()', fetch=True)
                try:
                    review_id = last[0][0]
                    run_query('INSERT INTO reviews (original_task_id, review_task_id, interval_days) VALUES (?, ?, ?)', (original_id, review_id, offset))
                    update_task_schedule(review_id, datetime.combine(review_date, datetime.min.time()))
                except Exception:
                    pass

    return scheduled_count

# ----------------- Missed rescheduler -----------------
def reschedule_missed(daily_minutes, lookahead_days=60):
    today = date.today()
    scheduled = fetch_tasks("scheduled_date IS NOT NULL")
    used = { (today + timedelta(days=i)).isoformat(): 0 for i in range(lookahead_days) }
    for _, r in scheduled.iterrows():
        if pd.isna(r['scheduled_date']):
            continue
        d = pd.to_datetime(r['scheduled_date']).date().isoformat()
        if d in used:
            used[d] += int(r['minutes'])
    missed = fetch_tasks("status = 'missed'")
    rescheduled = 0
    for _, r in missed.iterrows():
        tid = int(r['id'])
        mins = int(r['minutes'])
        for offset in range(1, lookahead_days+1):
            cand = (today + timedelta(days=offset)).isoformat()
            if used.get(cand, 0) + mins <= daily_minutes:
                update_task_schedule(tid, datetime.combine(datetime.fromisoformat(cand).date(), datetime.min.time()))
                used[cand] = used.get(cand, 0) + mins
                run_query('UPDATE tasks SET status = ? WHERE id = ?', ('pending', tid))
                rescheduled += 1
                break
    return rescheduled

# ----------------- Learning pattern helpers -----------------
def compute_subject_speed():
    """
    For each subject compute a multiplier = avg_actual_time / avg_estimated_time
    We return multiplier such that multiplier < 1 means user is faster -> reduce future estimates.
    If no data, return 1.0
    """
    rows = run_query('''SELECT t.subject, t.minutes, s.duration_minutes
                        FROM sessions s JOIN tasks t ON s.task_id = t.id''', fetch=True)
    if not rows:
        return {}
    df = pd.DataFrame(rows, columns=['subject','estimated','actual'])
    grouped = df.groupby('subject').agg({'estimated':'mean','actual':'mean'}).reset_index()
    multipliers = {}
    for _, r in grouped.iterrows():
        est = r['estimated'] if r['estimated']>0 else 1
        actual = r['actual'] if r['actual']>0 else est
        multipliers[r['subject']] = actual / est
    # clamp multipliers to [0.6, 1.6] to avoid extremes
    for k in multipliers:
        multipliers[k] = max(0.6, min(1.6, multipliers[k]))
    return multipliers

# ----------------- Analytics -----------------
def compute_stats():
    tasks = fetch_tasks()
    total = len(tasks)
    done = len(tasks[tasks['status']=='done'])
    missed = len(tasks[tasks['status']=='missed'])
    pending = len(tasks[tasks['status']=='pending'])
    rows = run_query('SELECT task_id, duration_minutes FROM sessions', fetch=True)
    time_spent = {}
    for task_id, dur in rows:
        t = run_query('SELECT subject FROM tasks WHERE id = ?', (task_id,), fetch=True)
        if t:
            subject = t[0][0]
            time_spent[subject] = time_spent.get(subject, 0) + dur
    progress = get_progress()
    return {'total': total, 'done': done, 'missed': missed, 'pending': pending, 'time_spent': time_spent, 'progress_map': progress}

# ----------------- Reset -----------------
def reset_all():
    if os.path.exists(DB):
        os.remove(DB)
    init_db(seed_demo=False)

# ----------------- UI -----------------
st.set_page_config(page_title="Smart Learning App", layout="wide")
init_db(seed_demo=True)

st.title("📚 Smart Learning App — Adaptive Scheduler & Coach")

# top controls
col_top_left, col_top_right = st.columns([3,1])
with col_top_left:
    st.markdown("**Manage tasks, take quizzes, and let the scheduler adapt to your habits.**")
with col_top_right:
    if st.button("Reset / Start Fresh"):
        reset_all()
        st.experimental_rerun()

# Sidebar for adding tasks and scheduler controls
with st.sidebar.expander("➕ Add Task / Topic", expanded=True):
    subject = st.text_input("Subject (course)", value="New Subject")
    topic = st.text_input("Topic / short description", value="Topic name")
    minutes = st.number_input("Estimated minutes", min_value=5, max_value=480, value=60, step=5)
    difficulty = st.slider("Difficulty (1 easy → 5 hard)", 1, 5, 3)
    priority = st.slider("Priority (1 low → 5 high)", 1, 5, 3)
    dl = st.date_input("Deadline", value=date.today() + timedelta(days=14))
    time_window = st.selectbox("Preferred time window (optional)", options=['any','morning','afternoon','evening'])
    if st.button("Add Task"):
        add_task(subject, topic, minutes, difficulty, priority, dl, status='pending', time_window=time_window)
        st.success("Task added")
        safe_rerun()

st.sidebar.markdown("---")
st.sidebar.header("Scheduler Controls")
daily_hours = st.sidebar.number_input("Available study hours / day", min_value=0.5, max_value=12.0, value=2.0, step=0.5)
daily_minutes = int(daily_hours * 60)
review_intervals = st.sidebar.multiselect("Spaced repetition intervals (days)", options=[1,2,3,5,7,14], default=[1,3,7])
lookahead = st.sidebar.number_input("Lookahead days", min_value=7, max_value=180, value=60)
if st.sidebar.button("Auto-generate schedule"):
    n = smart_schedule(daily_minutes, review_intervals, lookahead_days=int(lookahead))
    st.sidebar.success(f"Scheduled {n} tasks.")
if st.sidebar.button("Reschedule missed tasks"):
    r = reschedule_missed(daily_minutes, lookahead_days=int(lookahead))
    st.sidebar.success(f"Rescheduled {r} missed tasks.")

st.sidebar.markdown("---")
if st.sidebar.button("Export CSV"):
    rows = fetch_tasks()
    csv = rows.to_csv(index=False)
    st.sidebar.download_button("Download tasks.csv", data=csv, file_name="tasks.csv", mime="text/csv")
if st.sidebar.button("Export .ics Calendar"):
    rows = fetch_tasks("scheduled_date IS NOT NULL")
    cal = Calendar()
    for _, r in rows.iterrows():
        if pd.isna(r['scheduled_date']):
            continue
        e = Event()
        start_dt = pd.to_datetime(r['scheduled_date'])
        e.name = f"{r['subject']} — {r['topic']}"
        e.begin = start_dt.isoformat()
        e.duration = timedelta(minutes=int(r['minutes']))
        e.description = f"Status: {r['status']} | Diff: {r['difficulty']} | Prio: {r['priority']}"
        cal.events.add(e)
    f = io.StringIO(str(cal))
    st.sidebar.download_button("Download calendar.ics", data=f.getvalue(), file_name="study_schedule.ics", mime="text/calendar")

# Main area: schedule viewer
st.header("🗓 Schedule")
start = st.date_input("View start date", value=date.today())
days = st.slider("Days to show", min_value=3, max_value=90, value=14)
end = start + timedelta(days=days-1)
rows = fetch_tasks('scheduled_date IS NOT NULL AND date(scheduled_date) BETWEEN date(?) AND date(?)', (start.isoformat(), end.isoformat()))
if rows.empty:
    st.info("No scheduled tasks in this range. Use Auto-generate schedule or add tasks.")
else:
    rows['scheduled_day'] = pd.to_datetime(rows['scheduled_date']).dt.date
    for d in pd.date_range(start, end):
        dstr = d.date().isoformat()
        day_rows = rows[rows['scheduled_day'] == d.date()]
        st.subheader(dstr + f" — {len(day_rows)} item(s)")
        if day_rows.empty:
            st.write("No tasks")
            continue
        df_display = day_rows[['id','subject','topic','minutes','difficulty','priority','time_window','status']]
        st.table(df_display)
        for idx, r in day_rows.iterrows():
            cols = st.columns([4,1,1,1])
            cols[0].write(f"**{r['subject']}** — {r['topic']} ({r['minutes']} min) — window: {r['time_window']} — status: {r['status']}")
            if cols[1].button("Done", key=f"done_{int(r['id'])}"):
                # ask for actual duration optionally
                dur = st.number_input(f"Actual minutes spent on {r['topic']}", min_value=1, value=int(r['minutes']), key=f"dur_{int(r['id'])}")
                record_session(int(r['id']), int(dur))
                update_task_status(int(r['id']), 'done')
                # update progress if there's a quiz topic matching
                mark_topic_completed(r['topic'], 0)
                safe_rerun()
            if cols[2].button("Missed", key=f"missed_{int(r['id'])}"):
                update_task_status(int(r['id']), 'missed')
                safe_rerun()
            if cols[3].button("Reschedule", key=f"resch_{int(r['id'])}"):
                # simple reschedule to next day with capacity
                reschedule_missed(daily_minutes, lookahead_days=14)
                safe_rerun()

# Quizzes & progress area
st.markdown("---")
st.header("🧠 Quizzes & Progress")
progress_map = get_progress()
all_quiz_topics = [r[0] for r in run_query('SELECT DISTINCT topic FROM quizzes', fetch=True)]  # list of tuples -> pick first
all_quiz_topics = [t[0] for t in run_query('SELECT DISTINCT topic FROM quizzes', fetch=True)] if run_query('SELECT DISTINCT topic FROM quizzes', fetch=True) else []
# fallback if no quizzes in DB
if not all_quiz_topics:
    all_quiz_topics = ["Generators and Iterators","Calculus - Limits & Continuity"]

sel_topic = st.selectbox("Pick topic to take quiz / view progress", options=all_quiz_topics)
qs = fetch_quizzes_for_topic(sel_topic)
if not qs.empty:
    st.subheader(f"Quiz: {sel_topic}")
    score = 0
    for i, qrow in qs.iterrows():
        ans = st.text_input(f"Q{i+1}: {qrow['question']}", key=f"quiz_{qrow['id']}")
        if ans:
            if ans.strip().lower() == str(qrow['answer_text']).strip().lower():
                st.success("Correct!")
                score += 1
            else:
                st.info(f"Answer saved (correct: {qrow['answer_text']})")
    if st.button("Submit Quiz"):
        # record progress
        mark_topic_completed(sel_topic, score)
        st.success(f"Quiz recorded. Score: {score}/{len(qs)}")
        safe_rerun()
else:
    st.write("No quiz questions for this topic. You can add them via DB or seed data.")

# Dashboard
st.markdown("---")
st.header("📊 Dashboard")
stats = compute_stats()
c1,c2,c3,c4 = st.columns(4)
c1.metric("Total tasks", stats['total'])
c2.metric("Completed", stats['done'])
c3.metric("Missed", stats['missed'])
c4.metric("Pending", stats['pending'])

st.subheader("Time spent per subject (minutes)")
if stats['time_spent']:
    ts_df = pd.DataFrame(list(stats['time_spent'].items()), columns=['Subject','Minutes'])
    st.table(ts_df)
else:
    st.write("No sessions recorded yet.")

st.subheader("Upcoming deadlines (30 days)")
upcoming = fetch_tasks("date(deadline) BETWEEN date(?) AND date(?)", (date.today().isoformat(), (date.today()+timedelta(days=30)).isoformat()))
if upcoming.empty:
    st.write("No upcoming deadlines within 30 days.")
else:
    st.table(upcoming[['subject','topic','deadline','status']])

# Suggestions (simple)
st.markdown("---")
st.header("💡 Weekly Suggestions")
multipliers = compute_subject_speed()
if multipliers:
    st.write("Based on your recent sessions, these subjects are consistently faster/slower than estimated:")
    sug = [{"subject":k, "multiplier":v} for k,v in multipliers.items()]
    sug_df = pd.DataFrame(sug)
    st.table(sug_df)
    st.write("I recommend adjusting estimated minutes for subjects with multiplier < 0.9 (you're faster) or > 1.1 (you need more time).")
else:
    st.write("No session data yet — complete tasks to let the app learn your speed.")

st.info("Tip: Use 'Auto-generate schedule' after adding tasks. The scheduler will create review tasks for spaced repetition.")

# end
