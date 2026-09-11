def main():
    logging.info("Starting Web Server Thread...")
    threading.Thread(target=run_web, daemon=True).start()

    logging.info("Bourse Alert Bot started successfully.")

    # --- اجرای تست فوری در بدو راه‌اندازی ---
    logging.info("Running an immediate TEST pipeline on last market data...")
    try:
        run_pipeline()
        logging.info("Test pipeline completed successfully!")
    except Exception as e:
        logging.error(f"Error during test pipeline: {e}")

    while True:
        try:
            if is_market_open():
                logging.info("Market is OPEN. Running analysis...")
                run_pipeline()
                time.sleep(600)
            else:
                logging.info("Market is CLOSED. Sleeping for 60 seconds...")
                time.sleep(60)
        except Exception as e:
            logging.error(f"Unexpected error in main loop: {e}")
            time.sleep(30)
