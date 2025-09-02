import logging
import os
import sys
import base64
import uuid
import json
import csv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, ConversationHandler, CallbackQueryHandler,
)
import firebase_admin
from firebase_admin import credentials, firestore, storage

# --- Firebase Setup ---
try:
    cred_json_str = base64.b64decode(os.environ.get("SERVICE_ACCOUNT_BASE64")).decode("utf-8")
    cred_json = json.loads(cred_json_str)
    cred = credentials.Certificate(cred_json)
    firebase_admin.initialize_app(cred, {
        'storageBucket': f"{os.environ.get('FIREBASE_PROJECT_ID')}.appspot.com"
    })
    db = firestore.client()
    bucket = storage.bucket()
    print("Firebase successfully initialized.")
except Exception as e:
    print(f"Firebase initialization failed: {e}")
    sys.exit(1)

# --- Bot ki Basic Settings ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
PORT = int(os.environ.get('PORT', 8443))

# Conversation states
(CHOOSING_ACTION,
 CHOOSING_UPLOAD_METHOD, GETTING_SUBJECT, GETTING_TOPIC, GETTING_POLL, GETTING_CSV_TEXT,
 GETTING_NOTE_TITLE, GETTING_NOTE_PDF,
 GETTING_ARTICLE_TITLE, GETTING_ARTICLE_TEXT,
 GETTING_BANNER_IMAGE, CHOOSING_BANNER_TO_DELETE,
 GETTING_UPDATE_TEXT
) = range(13)


# --- Helper Functions ---
async def get_or_create_doc(collection_name, fields):
    collection_ref = db.collection(collection_name)
    query = collection_ref
    for key, value in fields.items():
        query = query.where(key, '==', value)
    docs = query.limit(1).get()
    if docs: return docs[0].id
    else: return collection_ref.add(fields)[1].id

# --- Main Conversation Logic ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [
        [InlineKeyboardButton("➕ Add Questions", callback_data='add_question')],
        [InlineKeyboardButton("📄 Add Note (PDF)", callback_data='add_note')],
        [InlineKeyboardButton("✍️ Add Article", callback_data='add_article')],
        [InlineKeyboardButton("🖼️ Add Banner", callback_data='add_banner')],
        [InlineKeyboardButton("🗑️ Remove Banner", callback_data='remove_banner')],
        [InlineKeyboardButton("🔔 Set Latest Update", callback_data='set_update')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    message_text = "Welcome, Admin! Please choose an action:"
    if update.message:
        await update.message.reply_text(message_text, reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup)
    return CHOOSING_ACTION

# --- Question Adding Flow ---
async def add_question_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    keyboard = [[InlineKeyboardButton("Forward Poll", callback_data='poll'), InlineKeyboardButton("Upload via CSV Text", callback_data='csv')]]
    await query.edit_message_text("How would you like to add questions?", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSING_UPLOAD_METHOD

async def choose_upload_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    context.user_data['upload_method'] = query.data
    await query.edit_message_text("Let's add questions. First, what is the subject? (e.g., History)")
    return GETTING_SUBJECT

async def receive_subject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['subject_name'] = update.message.text.strip()
    await update.message.reply_text(f"Great! Now, what is the topic within {context.user_data['subject_name']}?")
    return GETTING_TOPIC

async def receive_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['topic_name'] = update.message.text.strip()
    if context.user_data['upload_method'] == 'poll':
        await update.message.reply_text("Excellent. Now please send the poll.")
        return GETTING_POLL
    else:
        await update.message.reply_html("Excellent. Now send the questions as text, one per line:\n<code>Question,OptA,OptB,OptC,OptD,CorrectAnswer,Explanation</code>")
        return GETTING_CSV_TEXT

async def receive_poll_and_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    poll = update.message.poll
    if poll.type != 'quiz' or poll.correct_option_id is None:
        await update.message.reply_text("Error: Not a valid quiz poll."); return GETTING_POLL
    try:
        subject_id = await get_or_create_doc('subjects', {'name': context.user_data['subject_name']})
        topic_id = await get_or_create_doc('topics', {'name': context.user_data['topic_name'], 'subjectId': subject_id})
        db.collection('questions').add({
            "question": poll.question, "options": [opt.text for opt in poll.options],
            "correctAnswer": poll.options[poll.correct_option_id].text, "explanation": poll.explanation or "N/A",
            "subjectId": subject_id, "topicId": topic_id, "timestamp": firestore.SERVER_TIMESTAMP,
        })
        await update.message.reply_text("✅ Success! Question saved.")
    except Exception as e: await update.message.reply_text(f"An error occurred: {e}")
    return await start(update, context)

async def receive_csv_and_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lines = update.message.text.strip().split('\n'); saved_count = 0; errors = []
    try:
        subject_id = await get_or_create_doc('subjects', {'name': context.user_data['subject_name']})
        topic_id = await get_or_create_doc('topics', {'name': context.user_data['topic_name'], 'subjectId': subject_id})
        for i, line in enumerate(lines):
            try:
                parts = next(csv.reader([line])); parts = [p.strip() for p in parts]
                if not (6 <= len(parts) <= 7): errors.append(f"L{i+1}: Bad format"); continue
                question, *options, correct_answer = parts[:6]; explanation = parts[6] if len(parts) > 6 else "N/A"
                if correct_answer not in options: errors.append(f"L{i+1}: Correct answer not in options"); continue
                db.collection('questions').add({"question": question, "options": options, "correctAnswer": correct_answer, "explanation": explanation, "subjectId": subject_id, "topicId": topic_id, "timestamp": firestore.SERVER_TIMESTAMP}); saved_count += 1
            except Exception: errors.append(f"L{i+1}: Error processing line")
        report = f"✅ Process Complete! Saved: {saved_count}."
        if errors: report += "\n\nErrors on lines:\n- " + "\n- ".join(errors)
        await update.message.reply_text(report)
    except Exception as e: await update.message.reply_text(f"A major error occurred: {e}")
    return await start(update, context)

# --- Note Flow ---
async def add_note_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); await query.edit_message_text("What is the title for this PDF note?"); return GETTING_NOTE_TITLE
async def receive_note_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['note_title'] = update.message.text.strip(); await update.message.reply_text("Great. Now, please send the PDF file."); return GETTING_NOTE_PDF
async def receive_note_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        pdf_file = await update.message.document.get_file(); file_name = f"note_{uuid.uuid4()}.pdf"
        await update.message.reply_text("Uploading PDF..."); blob = bucket.blob(f"notes/{file_name}")
        blob.upload_from_string(await pdf_file.download_as_bytearray(), content_type='application/pdf'); blob.make_public()
        db.collection('notes').add({"title": context.user_data['note_title'], "fileUrl": blob.public_url, "timestamp": firestore.SERVER_TIMESTAMP})
        await update.message.reply_text("✅ Success! Note uploaded.")
    except Exception as e: await update.message.reply_text(f"An error occurred: {e}")
    return await start(update, context)

# --- Article Flow ---
async def add_article_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); await query.edit_message_text("What is the title for this article?"); return GETTING_ARTICLE_TITLE
async def receive_article_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['article_title'] = update.message.text.strip(); await update.message.reply_text("Great. Now, send the full text of the article."); return GETTING_ARTICLE_TEXT
async def receive_article_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        db.collection('articles').add({"title": context.user_data['article_title'], "content": update.message.text, "timestamp": firestore.SERVER_TIMESTAMP})
        await update.message.reply_text("✅ Success! Article has been saved.")
    except Exception as e: await update.message.reply_text(f"An error occurred: {e}")
    return await start(update, context)

# --- Banner Flow ---
async def add_banner_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); await query.edit_message_text("Please send the image for the new banner."); return GETTING_BANNER_IMAGE
async def receive_banner_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        photo_file = await update.message.photo[-1].get_file(); file_name = f"banner_{uuid.uuid4()}.jpg"
        await update.message.reply_text("Uploading banner..."); blob = bucket.blob(f"banners/{file_name}")
        blob.upload_from_string(await photo_file.download_as_bytearray(), content_type='image/jpeg'); blob.make_public()
        db.collection('banners').add({"imageUrl": blob.public_url, "fileName": file_name, "timestamp": firestore.SERVER_TIMESTAMP})
        await update.message.reply_text("✅ Success! Banner added.")
    except Exception as e: await update.message.reply_text(f"An error occurred: {e}")
    return await start(update, context)
async def remove_banner_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); banners = db.collection('banners').stream()
    keyboard = [[InlineKeyboardButton(f"Delete Banner {i+1}", callback_data=f"del_banner_{b.id}")] for i, b in enumerate(banners)]
    if not keyboard: await query.edit_message_text("No banners to remove."); return CHOOSING_ACTION
    await query.edit_message_text("Select a banner to delete:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSING_BANNER_TO_DELETE
async def delete_banner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); banner_id = query.data.split('_')[-1]
    try:
        banner_doc = db.collection('banners').document(banner_id).get()
        if banner_doc.exists:
            file_name = banner_doc.to_dict().get('fileName')
            if file_name: bucket.blob(f"banners/{file_name}").delete()
            db.collection('banners').document(banner_id).delete()
        await query.edit_message_text(f"✅ Success! Banner deleted.")
    except Exception as e: await query.edit_message_text(f"An error occurred: {e}")
    return await start(update, context)

# --- Update Flow ---
async def set_update_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); await query.edit_message_text("Send the new text for the 'Latest Updates' board."); return GETTING_UPDATE_TEXT
async def receive_update_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        db.collection('config').document('main').set({'notificationText': update.message.text}, merge=True)
        await update.message.reply_text("✅ Success! Latest update has been set.")
    except Exception as e: await update.message.reply_text(f"An error occurred: {e}")
    return await start(update, context)
    
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text('Operation cancelled.'); return ConversationHandler.END

if __name__ == '__main__':
    application = Application.builder().token(TOKEN).build()
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start), CallbackQueryHandler(start, pattern='^start_over$')],
        states={
            CHOOSING_ACTION: [
                CallbackQueryHandler(add_question_start, pattern='^add_question$'),
                CallbackQueryHandler(add_note_start, pattern='^add_note$'),
                CallbackQueryHandler(add_article_start, pattern='^add_article$'),
                CallbackQueryHandler(add_banner_start, pattern='^add_banner$'),
                CallbackQueryHandler(remove_banner_start, pattern='^remove_banner$'),
                CallbackQueryHandler(set_update_start, pattern='^set_update$'),
            ],
            CHOOSING_UPLOAD_METHOD: [CallbackQueryHandler(choose_upload_method, pattern='^(poll|csv)$')],
            GETTING_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_subject)],
            GETTING_TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_topic)],
            GETTING_POLL: [MessageHandler(filters.POLL, receive_poll_and_save)],
            GETTING_CSV_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_csv_and_save)],
            GETTING_NOTE_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_note_title)],
            GETTING_NOTE_PDF: [MessageHandler(filters.Document.PDF, receive_note_pdf)],
            GETTING_ARTICLE_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_article_title)],
            GETTING_ARTICLE_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_article_text)],
            GETTING_BANNER_IMAGE: [MessageHandler(filters.PHOTO, receive_banner_image)],
            CHOOSING_BANNER_TO_DELETE: [CallbackQueryHandler(delete_banner, pattern='^del_banner_')],
            GETTING_UPDATE_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_update_text)],
        },
        fallbacks=[CommandHandler("cancel", cancel)], allow_reentry=True
    )
    application.add_handler(conv_handler)
    print("Bot is starting webhook..."); application.run_webhook(listen="0.0.0.0", port=PORT, url_path=TOKEN, webhook_url=f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME')}/{TOKEN}")
