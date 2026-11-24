import pdb
import smtplib
import os
import datetime
import logging
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import pandas as pd
import getpass
import json
from .Read_config import config_read
from .Folder_creation import DirHandler
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.application import MIMEApplication
logger = logging.getLogger(__name__)
logging.getLogger('matplotlib').setLevel(logging.WARNING)

user_name = getpass.getuser().strip()
cr = config_read()
log_dirc = cr.log_dir("Log_Dir")
report_dirc = cr.report_dir("Report_Dir")

def read_email_config_from_ini():
    cr = config_read()
    email_settings = {}
    email_settings["email_host"] = cr.get_conf_data("EMAIL", "email_host")
    email_settings["email_from"] = cr.get_conf_data("EMAIL", "email_from")
    email_settings["email_to"] = cr.get_conf_data("EMAIL", "email_to")
    return email_settings

def make_dir(dir_path):
    DirHandler.mkdir(dir_path)
    return dir_path


def generate_pie_chart(passed, failed, not_run=0):
    labels = []
    sizes = []
    colors = []

    if passed:
        labels.append(f"Pass ({passed})")
        sizes.append(passed)
        colors.append('#4CAF50')

    if failed:
        labels.append(f"Fail ({failed})")
        sizes.append(failed)
        colors.append('#F44336')

    if not_run:
        labels.append(f"Not Run ({not_run})")
        sizes.append(not_run)
        colors.append('#2196F3')

    fig, ax = plt.subplots(figsize=(3.5, 3.5))
    ax.pie(sizes, labels=labels, startangle=90, colors=colors)
    ax.axis('equal')

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    chart_path = f"trial_logs/validation_chart_{timestamp}.png"
    os.makedirs("trial_logs", exist_ok=True)

    plt.savefig(chart_path, bbox_inches='tight')
    plt.close()
    return chart_path

def flatten_test_logs(master_logs):

    if not master_logs:
        return []

    rows = []
    for log in master_logs:
        # If log is structured as {'M3P': {...}, 'FIN': {...}}
        if isinstance(log, dict) and all(isinstance(v, dict) for v in log.values()):
            for module_name, module_log in log.items():
                module = module_log.get("Module", module_name)
                stage = module_log.get("Stage")
                cycle = module_log.get("Cycle")
                log_id = module_log.get("DataValidationLog_ID")
                test_cases = module_log.get("TestCases", [])
                for test in test_cases:
                    rows.append({
                        "Environment": "",  # You can fill this in if available
                        "DataValidationLog_ID": log_id,
                        "Module": module,
                        "Stage": stage,
                        "Cycle": cycle,
                        "TestCaseID": test.get("TestCaseID"),
                        "TestCaseName": test.get("TestCaseName"),
                        "StartTime": test.get("StartTime"),
                        "EndTime": test.get("EndTime"),
                        "Result": test.get("Result"),
                        "MismatchFields": ", ".join(test.get("MismatchFields", [])),
                        "MissingFields": ", ".join(test.get("MissingFields", []))
                    })
        else:
            module = log.get("Module")
            stage = log.get("Stage")
            cycle = log.get("Cycle")
            for test_case in log.get("TestCases", []):
                rows.append({
                    "Module": module,
                    "Stage": stage,
                    "Cycle": cycle,
                    "TestCaseID": test_case.get("TestCaseID"),
                    "TestCaseName": test_case.get("TestCaseName"),
                    "StartTime": test_case.get("StartTime"),
                    "EndTime": test_case.get("EndTime"),
                    "Result": test_case.get("Result"),
                    "MismatchFields": test_case.get("MismatchFields", []),
                    "MissingFields": test_case.get("MissingFields", []),
                    "DataValidationLog_ID": log.get("DataValidationLog_ID"),
                    "Environment": log.get("Environment", "")
                })
    return rows

def generate_bar_chart(master_log_rows,log_base_name):

    try:
        # Keep module as a whole string (no splitting)
        data = [
            {
                "Module": row.get("Module", "").strip(),
                "Result": row.get("Result", "").capitalize()
            }
            for row in master_log_rows
        ]
        df = pd.DataFrame(data)
        if df.empty:
            print("No data available to generate bar chart.")
            return None

        # Group by Module and Result (Pass/Fail)
        grouped = df.groupby(["Module", "Result"]).size().unstack(fill_value=0)

        fig, ax = plt.subplots(figsize=(4, 2.5))
        grouped.plot(
            kind='bar',
            stacked=True,
            ax=ax,
            color={"Pass": "#A8E6A1", "Fail": "#F8A1A4"},
            width=0.4
        )

        ax.set_title("Test Results by Module", fontsize=9)
        ax.set_xlabel("Module", fontsize=8)
        ax.set_ylabel("Count", fontsize=7)
        plt.xticks(rotation=0, fontsize=7)
        plt.yticks(fontsize=7)
        ax.yaxis.set_major_locator(ticker.MaxNLocator(integer=True))

        # Add value labels on bars
        # for container in ax.containers:
        #     ax.bar_label(container, fmt='%d', label_type='center', fontsize=8)
        for container in ax.containers:
            labels=[int(bar.get_height()) if bar.get_height() > 0 else '' for bar in container]
            ax.bar_label(container,labels=labels,label_type='center',fontsize=8)
        plt.tight_layout()

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        log_dir = make_dir(log_dirc)
        log_dir = make_dir(os.path.join(log_dir, "Barchart"))
        chart_path = os.path.join(log_dir, f"{log_base_name}.png")
        # chart_path = os.path.join(log_dir, f"test_results_bar_chart_{timestamp}.png")
        plt.savefig(chart_path, bbox_inches='tight', dpi=120)
        plt.close()
        return chart_path
    except Exception as e:
        logger.error(f"Unexpected error in write_master_log_to_file: {e}")
        return None


def generate_bar_chart2(master_log_rows,log_base_name):
    import pandas as pd
    df = pd.DataFrame(master_log_rows)
    grouped = df.groupby(["Module", "Result"]).size().unstack(fill_value=0)

    fig, ax = plt.subplots(figsize=(4, 2.5))
    grouped.plot(kind='bar', stacked=True, ax=ax, color={"Pass": "#A8E6A1", "Fail": "#F8A1A4"}, width=0.1)
    ax.set_title("Test Results by Module", fontsize=9)
    ax.set_xlabel("Module", fontsize=8)
    ax.set_ylabel("Count", fontsize=7)
    plt.xticks(rotation=0, fontsize=7)
    plt.yticks(fontsize=7)
    ax.yaxis.set_major_locator(ticker.MaxNLocator(integer=True))
    plt.tight_layout()
    for c in ax.containers:
        labels = [v.get_height() if v.get_height() > 0 else 0 for v in c]
        ax.bar_label(c, labels=[f'{x:.0f}' for x in labels], label_type='center', fontsize=12)

    # timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = make_dir(log_dirc)
    log_dir=make_dir(os.path.join(log_dir, "Barchart"))
    chart_path = os.path.join(log_dir, f"{log_base_name}.png")
    # os.makedirs("trial_logs", exist_ok=True)

    plt.savefig(chart_path, bbox_inches='tight', dpi=120)
    # plt.show()
    plt.close()
    return chart_path


def send_email_report(
        recipients,
        subject,
        master_log_rows,
        attachment_path,
        sender_email,
        smtp_server,
        smtp_port=25,
        report_type="email"
):
    try:
        total = len(master_log_rows)
        passed = sum(1 for row in master_log_rows if row["Result"].lower() == "pass")
        failed = total - passed
        not_run = 0

        # chart_path = generate_pie_chart(passed, failed, not_run)

        env = master_log_rows[0].get("Environment", "")
        # log_id = master_log_rows[0].get("DataValidationLog_ID", "")

        log_ids=list({str(row.get("DataValidationLog_ID")) for row in master_log_rows if row.get("DataValidationLog_ID")})
        log_id=",".join(log_ids) if log_ids else None
        # log_dir = make_dir(log_dirc)
        chart_path=""
        if os.path.exists(attachment_path):
            with open(attachment_path, "r", encoding="utf-8") as f:
                full_master_log = json.load(f)
                log_base_name = os.path.splitext(os.path.basename(attachment_path))[0]
                log_file_name = f"{log_base_name}.txt"

        if isinstance(full_master_log, dict):
            full_master_log = [full_master_log]
            master_log_rows = flatten_test_logs(full_master_log)
            if log_base_name:
                chart_path = generate_bar_chart(master_log_rows,log_base_name)
        elif isinstance(full_master_log,list):
            if log_base_name:
                chart_path = generate_bar_chart(master_log_rows,log_base_name)

        if report_type == "html":
            log_tag = ""
            img_tag = f'<img src="{chart_path}" style="display:block;margin:0;padding-left: 300;" alt="Validation Chart">'
            log_tag = f'<h3><a href="{attachment_path}" download> {log_file_name}</a></h3>'
            img_style_1 = f'<td style="width:20%;padding-left:30px; vertical-align:middle;">'
            img_style_2 = f'<td style="width:80%;margin:0 text-align:left; vertical-align:middle;">'
            table_tag = f'<table border="1" cellpadding="6" cellspacing="0" style="width:95%;border-collapse:collapse;font-family:Arial,sans-serif;font-size:11px;margin-left:auto;margin-right:auto">'

        elif report_type == "email":
            img_tag = f'<img src="cid:image1" style="display:block; margin:0;padding-left: 450;" alt="Validation Chart">'
            log_tag = ""
            img_style_1 = f'<td style="width:30%;padding-left:30px; vertical-align:middle;">'
            img_style_2 = f'<td style="width:70%;padding-left:100px;margin:0 text-align:left; vertical-align:middle;">'
            table_tag = f'<table border="1" cellpadding="6" cellspacing="0" style="width:95%;border-collapse:collapse;font-family:Arial,sans-serif;font-size:11px;margin-left:30px;">'
        else:
            raise ValueError("Invalid report_type. Use 'html' or 'email'.")

        html_body_report = f""" 
        <h2 style="color:#2e6c80; text-align:center;">Data Validation Summary</h2>
        <table style="width:100%;font-family:Arial,sans-serif; margin-bottom:10px; table-layout:auto;">
        <tr>
        {log_tag}
        </tr>
        <tr>
            {img_style_1}
                <div style="line-height:1.6; font-size:14px;">
                    Environment: {env}<br>
                    DataValidationLog_ID: {log_id}<br>
                    Executed: {total}<br>
                    Pass: {passed} &nbsp;&nbsp;
                    Fail: {failed}<br>
                     <span style="white-space:nowrap;">Executed By:{user_name}</span>
                </div>
            </td>

            {img_style_2}
                 {img_tag}
            </td>
        </tr>
        </table>

        <hr>
        {table_tag}
        <thead style="background-color:#d9eaf7;">
        <tr>
            <th style="font-size:14px;backgroun-color:#f2f2f2;color:#708090;">Module</th>
            <th style="font-size:14px;backgroun-color:#f2f2f2;color:#708090;">Stage</th>
            <th style="font-size:14px;backgroun-color:#f2f2f2;color:#708090;">Cycle</th>
            <th style="font-size:14px;backgroun-color:#f2f2f2;color:#708090;">TestCaseID</th>
            <th style="font-size:14px;backgroun-color:#f2f2f2;color:#708090;">TestCaseName</th>
            <th style="font-size:14px;backgroun-color:#f2f2f2;color:#708090;">StartTime</th>
            <th style="font-size:14px;backgroun-color:#f2f2f2;color:#708090;">EndTime</th>
            <th style="font-size:14px;backgroun-color:#f2f2f2;color:#708090;">Test Result</th>
            <th style="font-size:14px;backgroun-color:#f2f2f2;color:#708090;">MisMatch</th>
            <th style="font-size:14px;backgroun-color:#f2f2f2;color:#708090;">Missing</th>
        </tr>
        </thead>
        <tbody>
        """

        html_body_email = f"""
        <h2 style="color:#2e6c80; text-align:center;">Data Validation Summary</h2>
        <table style="width:100%;font-family:Arial,sans-serif; margin-bottom:10px; table-layout:auto;">
        <tr>
            <td style="width:30%;padding-left:30px; vertical-align:middle;">
                <div style="line-height:1.6; font-size:14px;">
                    <strong>Environment:</strong> {env}<br>
                    <strong>DataValidationLog_ID:</strong> {log_id}<br>
                    <strong>Executed:</strong> {total}<br>
                    <strong>Pass:</strong> {passed} &nbsp;&nbsp;
                    <strong>Fail:</strong> {failed}<br>
                    <strong>Executed By:</strong> {user_name}
                </div>
            </td>

            <td style="width:70%;padding-left:100px;margin:0 text-align:left; vertical-align:middle;">
                 <img src="cid:image1" style="display:block; margin:0;padding-left: 450;" alt="Validation Chart">
            </td>
        </tr>
        </table>

        <hr>
        <table border="1" cellpadding="6" cellspacing="0" style="width:95%;border-collapse:collapse;font-family:Arial,sans-serif;font-size:11px;margin-left:30px;">
        <thead style="background-color:#d9eaf7;">
        <tr>
            <th style="font-size:14px;backgroun-color:#f2f2f2;">Module</th>
            <th style="font-size:14px;backgroun-color:#f2f2f2;">Stage</th>
            <th style="font-size:14px;backgroun-color:#f2f2f2;">Cycle</th>
            <th style="font-size:14px;backgroun-color:#f2f2f2;">TestCaseID</th>
            <th style="font-size:14px;backgroun-color:#f2f2f2;">TestCaseName</th>
            <th style="font-size:14px;backgroun-color:#f2f2f2;">StartTime</th>
            <th style="font-size:14px;backgroun-color:#f2f2f2;">EndTime</th>
            <th style="font-size:14px;backgroun-color:#f2f2f2;">Test Result</th>
            <th style="font-size:14px;backgroun-color:#f2f2f2;">MisMatch</th>
            <th style="font-size:14px;backgroun-color:#f2f2f2;">Missing</th>
        </tr>
        </thead>
        <tbody>
        """

        for row in master_log_rows:
            result = row.get("Result", "").capitalize()
            bgcolor = "#c6efce" if result == "Pass" else "#ffc7ce"
            html_body_report += f"""
               <!-- # <tr style="background-color:{bgcolor};"> # commented as it applies to entire row, now it applies to only result column -->
                <tr>
                    <td>{row.get("Module", "")}</td>
                    <td>{row.get("Stage", "")}</td>
                    <td>{row.get("Cycle", "")}</td>
                    <td>{row.get("TestCaseID", "")}</td>
                    <td style="max-width:200px; word-wrap:break-word; white-space:normal;">{row.get("TestCaseName", "")}</td>
                    <td>{row.get("StartTime", "")}</td>
                    <td>{row.get("EndTime", "")}</td>
                    <!-- <td>{result}</td>   #commented to add color to only result added new line below -->
                    <td style="background-color:{bgcolor}; font-weight:bold;">{result}</td>
                    <td>{row.get("MismatchFields", "")}</td>
                    <td>{row.get("MissingFields", "")}</td>
                </tr>
            """
            html_body_email += f"""
               <!-- # <tr style="background-color:{bgcolor};"> # commented as it applies to entire row, now it applies to only result column -->
                <tr>
                    <td>{row.get("Module", "")}</td>
                    <td>{row.get("Stage", "")}</td>
                    <td>{row.get("Cycle", "")}</td>
                    <td>{row.get("TestCaseID", "")}</td>
                    <td style="max-width:200px; word-wrap:break-word; white-space:normal;">{row.get("TestCaseName", "")}</td>
                    <td>{row.get("StartTime", "")}</td>
                    <td>{row.get("EndTime", "")}</td>
                    <!-- <td>{result}</td>   #commented to add color to only result added new line below -->
                    <td style="background-color:{bgcolor}; font-weight:bold;">{result}</td>
                    <td>{row.get("MismatchFields", "")}</td>
                    <td>{row.get("MissingFields", "")}</td>
                </tr>
            """
        html_body_report += "</tbody></table><br><br><br>"
        html_body_email += "</tbody></table><br><br><br>"

        # Email structure
        msg = MIMEMultipart("related")
        msg["Subject"] = subject
        msg["From"] = sender_email
        msg["To"] = ", ".join(recipients)

        msg_alt = MIMEMultipart("alternative")
        msg.attach(msg_alt)
        msg_alt.attach(MIMEText(html_body_email, "html"))

        # Embed chart inline
        with open(chart_path, "rb") as img:
            img_data = img.read()
            image = MIMEImage(img_data, name=os.path.basename(chart_path))
            image.add_header("Content-ID", "<image1>")
            image.add_header("Content-Disposition", "inline", filename=os.path.basename(chart_path))
            msg.attach(image)

        # Attach log JSON
        if os.path.exists(attachment_path):
            with open(attachment_path, "rb") as f:
                file_data = f.read()
                base_name = os.path.splitext(os.path.basename(attachment_path))[0]
                file_name = f"{base_name}.txt"
                attachment = MIMEApplication(file_data, Name=file_name)
                attachment["Content-Disposition"] = f'attachment; filename="{file_name}"'
                msg.attach(attachment)
        else:
            print(f"Log file not found: {attachment_path}")

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        report_dir = make_dir(report_dirc)

        if report_type == "html":
            # Saving as html
            filename = f"{log_base_name}.html"
            with open(report_dir + filename, 'w', encoding="utf-8") as preview_html:
                preview_html.write(html_body_report)
                print("HTML Report Saved")
        elif report_type == "email":
            # Saving as email
            filename = f"Validation Report_{timestamp}.eml"
            with open(report_dir + filename, 'w', encoding='utf-8') as preview:
                preview.write(msg.as_string())
                print("Email Report Saved")
        else:
            # Invalid Input Value
            print("No Report Generated")

        # Send email
        # with smtplib.SMTP(smtp_server, smtp_port) as server:
        # server.send_message(msg)

        print("Email sent successfully")

    except Exception as e:
        print(f"Failed to send email: {e}")
