import platform
import argparse
import time
import json
import re
import os
import shutil
import logging


from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains

from prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_TEXT_ONLY, SYSTEM_PREVIOUS_STEP, ERROR_GROUNDING_AGENT_PROMPT, SYSTEM_ORCHESTRATION
from openai import OpenAI
from utils import get_web_element_rect, encode_image, extract_information, print_message,\
    get_webarena_accessibility_tree, get_pdf_retrieval_ans_from_assistant, clip_message_and_obs, clip_message_and_obs_text_only

def setup_logger(folder_path):
    log_file_path = os.path.join(folder_path, 'agent.log')

    logger = logging.getLogger()
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()

    handler = logging.FileHandler(log_file_path, encoding="utf-8")
    formatter = logging.Formatter('%(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def driver_config(args):
    options = webdriver.ChromeOptions()
    options.add_argument("disable-blink-features=AutomationControlled")

    if args.save_accessibility_tree:
        args.force_device_scale = True

    if args.force_device_scale:
        options.add_argument("--force-device-scale-factor=1")
    if args.headless:
        options.add_argument("--headless")
        options.add_argument(
            "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
        )
    options.add_experimental_option(
        "prefs", {
            "download.default_directory": args.download_dir,
            "plugins.always_open_pdf_externally": True
        }
    )
    return options


def format_msg(it, init_msg, pdf_obs, warn_obs, web_img_b64, web_text, prev_step_action=""):
    if it == 1:
        init_msg += f"{prev_step_action}\nI've provided the tag name of each element and the text it contains (if text exists). Note that <textarea> or <input> may be textbox, but not exactly. Please focus more on the screenshot and then refer to the textual information.\n{web_text}"
        init_msg_format = {
            'role': 'user',
            'content': [
                {'type': 'text', 'text': init_msg},
            ]
        }
        init_msg_format['content'].append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{web_img_b64}"}
        })
        return init_msg_format
    else:
        if not pdf_obs:
            curr_msg = {
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': f"{prev_step_action}\nObservation:{warn_obs} please analyze the attached screenshot and give the Thought and Action. I've provided the tag name of each element and the text it contains (if text exists). Note that <textarea> or <input> may be textbox, but not exactly. Please focus more on the screenshot and then refer to the textual information.\n{web_text}"},
                    {
                        'type': 'image_url',
                        'image_url': {"url": f"data:image/png;base64,{web_img_b64}"}
                    }
                ]
            }
        else:
            curr_msg = {
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': f"{prev_step_action}\nObservation: {pdf_obs} Please analyze the response given by Assistant, then consider whether to continue iterating or not. The screenshot of the current page is also attached, give the Thought and Action. I've provided the tag name of each element and the text it contains (if text exists). Note that <textarea> or <input> may be textbox, but not exactly. Please focus more on the screenshot and then refer to the textual information.\n{web_text}"},
                    {
                        'type': 'image_url',
                        'image_url': {"url": f"data:image/png;base64,{web_img_b64}"}
                    }
                ]
            }
        return curr_msg


def format_msg_text_only(it, init_msg, pdf_obs, warn_obs, ac_tree, prev_step_action=""):
    if it == 1:
        init_msg_format = {
            'role': 'user',
            'content': init_msg + '\n' + ac_tree
        }
        return init_msg_format
    else:
        if not pdf_obs:
            curr_msg = {
                'role': 'user',
                'content': f"{prev_step_action}\nObservation:{warn_obs} please analyze the accessibility tree and give the Thought and Action.\n{ac_tree}"
            }
        else:
            curr_msg = {
                'role': 'user',
                'content': f"{prev_step_action}\nObservation: {pdf_obs} Please analyze the response given by Assistant, then consider whether to continue iterating or not. The accessibility tree of the current page is also given, give the Thought and Action.\n{ac_tree}"
            }
        return curr_msg


def call_gpt4v_api(args, openai_client, messages):
    retry_times = 0
    while True:
        try:
            if not args.text_only:
                logging.info('Calling gpt4v API...')
                openai_response = openai_client.chat.completions.create(
                    model=args.api_model, messages=messages, max_tokens=1000, seed=args.seed
                )
            else:
                logging.info('Calling gpt4 API...')
                openai_response = openai_client.chat.completions.create(
                    model=args.api_model, messages=messages, max_tokens=1000, seed=args.seed, timeout=30
                )

            prompt_tokens = openai_response.usage.prompt_tokens
            completion_tokens = openai_response.usage.completion_tokens

            logging.info(f'Prompt Tokens: {prompt_tokens}; Completion Tokens: {completion_tokens}')

            gpt_call_error = False
            return prompt_tokens, completion_tokens, gpt_call_error, openai_response

        except Exception as e:
            logging.info(f'Error occurred, retrying. Error type: {type(e).__name__}')

            if type(e).__name__ == 'RateLimitError':
                time.sleep(10)

            elif type(e).__name__ == 'APIError':
                time.sleep(15)

            elif type(e).__name__ == 'InvalidRequestError':
                gpt_call_error = True
                return None, None, gpt_call_error, None

            else:
                gpt_call_error = True
                return None, None, gpt_call_error, None

        retry_times += 1
        if retry_times == 10:
            logging.info('Retrying too many times')
            return None, None, True, None


def exec_action_click(info, web_ele, driver_task):
    driver_task.execute_script("arguments[0].setAttribute('target', '_self')", web_ele)
    web_ele.click()
    time.sleep(3)


def exec_action_type(info, web_ele, driver_task):
    warn_obs = ""
    type_content = info['content']

    ele_tag_name = web_ele.tag_name.lower()
    ele_type = web_ele.get_attribute("type")
    # outer_html = web_ele.get_attribute("outerHTML")
    if (ele_tag_name != 'input' and ele_tag_name != 'textarea') or (ele_tag_name == 'input' and ele_type not in ['text', 'search', 'password', 'email', 'tel']):
        warn_obs = f"note: The web element you're trying to type may not be a textbox, and its tag name is <{web_ele.tag_name}>, type is {ele_type}."
    try:
        # Not always work to delete
        web_ele.clear()
        # Another way to delete
        if platform.system() == 'Darwin':
            web_ele.send_keys(Keys.COMMAND + "a")
        else:
            web_ele.send_keys(Keys.CONTROL + "a")
        web_ele.send_keys(" ")
        web_ele.send_keys(Keys.BACKSPACE)
    except:
        pass

    actions = ActionChains(driver_task)
    actions.click(web_ele).perform()
    actions.pause(1)

    try:
        driver_task.execute_script("""window.onkeydown = function(e) {if(e.keyCode == 32 && e.target.type != 'text' && e.target.type != 'textarea' && e.target.type != 'search') {e.preventDefault();}};""")
    except:
        pass

    actions.send_keys(type_content)
    actions.pause(2)

    actions.send_keys(Keys.ENTER)
    actions.perform()
    time.sleep(10)
    return warn_obs


def exec_action_scroll(info, web_eles, driver_task, args, obs_info):
    scroll_ele_number = info['number']
    scroll_content = info['content']
    if scroll_ele_number == "WINDOW":
        if scroll_content == 'down':
            driver_task.execute_script(f"window.scrollBy(0, {args.window_height*2//3});")
        else:
            driver_task.execute_script(f"window.scrollBy(0, {-args.window_height*2//3});")
    else:
        if not args.text_only:
            scroll_ele_number = int(scroll_ele_number)
            web_ele = web_eles[scroll_ele_number]
        else:
            element_box = obs_info[scroll_ele_number]['union_bound']
            element_box_center = (element_box[0] + element_box[2] // 2, element_box[1] + element_box[3] // 2)
            web_ele = driver_task.execute_script("return document.elementFromPoint(arguments[0], arguments[1]);", element_box_center[0], element_box_center[1])
        actions = ActionChains(driver_task)
        driver_task.execute_script("arguments[0].focus();", web_ele)
        if scroll_content == 'down':
            actions.key_down(Keys.ALT).send_keys(Keys.ARROW_DOWN).key_up(Keys.ALT).perform()
        else:
            actions.key_down(Keys.ALT).send_keys(Keys.ARROW_UP).key_up(Keys.ALT).perform()
    time.sleep(3)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--test_file', type=str, default='data/test.json')
    parser.add_argument('--max_iter', type=int, default=5)
    parser.add_argument('--trajectory', action='store_true')
    parser.add_argument('--error_max_reflection_iter', type=int, default=1, help='Number of reflection restarts allowed when exceeding max_iter')
    
    parser.add_argument("--api_key", default="key", type=str, help="YOUR_OPENAI_API_KEY")
    parser.add_argument("--api_model", default="gpt-4-vision-preview", type=str, help="api model name")
    parser.add_argument("--output_dir", type=str, default='results')
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--max_attached_imgs", type=int, default=1)
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--download_dir", type=str, default="downloads")
    parser.add_argument("--text_only", action='store_true')
    # for web browser
    parser.add_argument("--headless", action='store_true', help='The window of selenium')
    parser.add_argument("--save_accessibility_tree", action='store_true')
    parser.add_argument("--force_device_scale", action='store_true')
    parser.add_argument("--window_width", type=int, default=1024)
    parser.add_argument("--window_height", type=int, default=768)  # for headless mode, there is no address bar
    parser.add_argument("--fix_box_color", action='store_true')
    parser.add_argument("--start_maximized", action='store_true')

    args = parser.parse_args()

    # OpenAI client
    client = OpenAI(api_key=args.api_key)
    import undetected_chromedriver as uc
    options = driver_config(args)
    options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36')
    options.add_argument("disable-blink-features=AutomationControlled")
    # Save Result file
    current_time = time.strftime("%Y%m%d_%H_%M_%S", time.localtime())
    result_dir = os.path.join(args.output_dir, current_time)
    os.makedirs(result_dir, exist_ok=True)

    # Load tasks
    tasks = []
    with open(args.test_file, 'r', encoding='utf-8') as f:
        for line in f:
            tasks.append(json.loads(line))


    for task_id in range(len(tasks)):
        task = tasks[task_id]
        task_dir = os.path.join(result_dir, 'task{}'.format(task["id"]))
        os.makedirs(task_dir, exist_ok=True)
        setup_logger(task_dir)
        logging.info(f'########## TASK{task["id"]} ##########')

        # Initialize the window (The window was too small, and I couldn't see some parts of the screen, so I added the --start_maximized parameter).
        driver_task = webdriver.Chrome(options=options)
        if args.start_maximized: driver_task.maximize_window()
        else: driver_task.set_window_size(args.window_width, args.window_height)  # larger height may contain more web information
        

        # About window size, 765 tokens
        # You can resize to height = 512 by yourself (255 tokens, Maybe bad performance)
        driver_task.get(task['web'])
        try:
            driver_task.find_element(By.TAG_NAME, 'body').click()
        except:
            pass
        # sometimes enter SPACE, the page will sroll down
        driver_task.execute_script("""window.onkeydown = function(e) {if(e.keyCode == 32 && e.target.type != 'text' && e.target.type != 'textarea') {e.preventDefault();}};""")
        time.sleep(5)

        # We only deal with PDF file
        for filename in os.listdir(args.download_dir):
            file_path = os.path.join(args.download_dir, filename)
            if os.path.isfile(file_path):
                os.remove(file_path)

        download_files = []  # sorted(os.listdir(args.download_dir))

        fail_obs = ""  # When error execute the action
        pdf_obs = ""  # When download PDF file
        warn_obs = ""  # Type warning
        pattern = r'Thought:|Action:|Observation:|Errors:|Explanation:'

        messages = [{'role': 'system', 'content': SYSTEM_PROMPT}]
        obs_prompt = "Observation: please analyze the attached screenshot and give the Thought and Action. "
        if args.text_only:
            messages = [{'role': 'system', 'content': SYSTEM_PROMPT_TEXT_ONLY}]
            obs_prompt = "Observation: please analyze the accessibility tree and give the Thought and Action."

        init_msg = f"""Now given a task: {task['ques']}  Please interact with https://www.example.com and get the answer. \n"""
        init_msg = init_msg.replace('https://www.example.com', task['web'])
        init_msg = init_msg + obs_prompt

        it = 0
        accumulate_prompt_token = 0
        accumulate_completion_token = 0

        # Error Grounding Agent
        activate_EGA=True
        error_exist=False
        EGA_explanation=""
        bot_thought=""
        
        # Reflection: Trajectory
        current_history = ""     # Record the steps of the current iteration.
        
        print(f"Trajectory: {args.trajectory}")
        print(f"EGA: {activate_EGA}")
        
        log_records = []  # 儲存所有 Agent 操作紀錄
        orchestrator_logs = [] # 儲存 Orchestrator Agent 的操作紀錄
        while it < args.max_iter:
            logging.info(f'Iter: {it}')
            it += 1
            if not fail_obs:
                try:
                    if not args.text_only:
                        rects, web_eles, web_eles_text = get_web_element_rect(driver_task, fix_color=args.fix_box_color)
                    else:
                        accessibility_tree_path = os.path.join(task_dir, 'accessibility_tree{}'.format(it))
                        ac_tree, obs_info = get_webarena_accessibility_tree(driver_task, accessibility_tree_path)

                except Exception as e:
                    if not args.text_only:
                        logging.error('Driver error when adding set-of-mark.')
                    else:
                        logging.error('Driver error when obtaining accessibility tree.')
                    logging.error(e)
                    break
            
                img_path = os.path.join(task_dir, 'screenshot{}.png'.format(it))
                driver_task.save_screenshot(img_path)
                
                # encode image
                b64_img = encode_image(img_path)

                # Error Grounding Agent
                if it>1 and activate_EGA:
                    # 丟 ground agent prompt 和 screenshot
                    EGA_messages = [{'role': 'system', 'content': ERROR_GROUNDING_AGENT_PROMPT}]
                    EGA_img = encode_image(img_path)
                    EGA_user_messages={
                        'role': 'user', 
                        'content':[
                            {'type':'text', 'text':'Thought:'+bot_thought+'\nScreenshot:'},
                            {
                                'type': 'image_url',
                                'image_url': {"url": f"data:image/png;base64,{EGA_img}"}
                            }
                        ]}
                    EGA_messages.append(EGA_user_messages)
                    prompt_tokens, completion_tokens, gpt_call_error, openai_response = call_gpt4v_api(args, client, EGA_messages)
                    if gpt_call_error:
                        break
                    else:
                        accumulate_prompt_token += prompt_tokens
                        accumulate_completion_token += completion_tokens
                        logging.info(f'Accumulate Prompt Tokens: {accumulate_prompt_token}; Accumulate Completion Tokens: {accumulate_completion_token}')
                        logging.info('API call complete...')
                    EGA_res = openai_response.choices[0].message.content
                    if re.split(pattern, EGA_res)[1].strip() == 'Yes':
                        error_exist = True
                    elif re.split(pattern, EGA_res)[1].strip() == 'No':
                        error_exist = False
                    else:
                        error_exist = False
                        print("error_exist got unexpected result:",EGA_res)
                    if error_exist==True:
                        EGA_explanation = re.split(pattern, EGA_res)[2].strip()
                        
                # accessibility tree
                if (not args.text_only) and args.save_accessibility_tree:
                    accessibility_tree_path = os.path.join(task_dir, 'accessibility_tree{}'.format(it))
                    get_webarena_accessibility_tree(driver_task, accessibility_tree_path)

                # format msg
                if not args.text_only:
                    curr_msg = format_msg(it, init_msg, pdf_obs, warn_obs, b64_img, web_eles_text, SYSTEM_PREVIOUS_STEP + current_history)
                    if error_exist == True:
                        curr_msg['content'][0]['text']+=("\nAdditional Information: Looks like your previous thought has some problem in operation. Here is the message from Error Grounding Agent\n"+EGA_explanation)
                else:
                    curr_msg = format_msg_text_only(it, init_msg, pdf_obs, warn_obs, ac_tree, SYSTEM_PREVIOUS_STEP + current_history)
                    if error_exist == True:
                        curr_msg['content']+=("\nAdditional Information: Looks like your previous thought has some problem in operation. Here is the message from Error Grounding Agent\n"+EGA_explanation)
                messages.append(curr_msg)
            else:
                curr_msg = {
                    'role': 'user',
                    'content': fail_obs
                }
                messages.append(curr_msg)

            # Clip messages, too many attached images may cause confusion
            if not args.text_only:
                messages = clip_message_and_obs(messages, args.max_attached_imgs)
            else:
                messages = clip_message_and_obs_text_only(messages, args.max_attached_imgs)

            # Call GPT-4v API
            prompt_tokens, completion_tokens, gpt_call_error, openai_response = call_gpt4v_api(args, client, messages)
        
            if gpt_call_error:
                break
            else:
                accumulate_prompt_token += prompt_tokens
                accumulate_completion_token += completion_tokens
                logging.info(f'Accumulate Prompt Tokens: {accumulate_prompt_token}; Accumulate Completion Tokens: {accumulate_completion_token}')
                logging.info('API call complete...')
            # 取得 GPT-4v 多候選行為輸出
            gpt_4v_res = openai_response.choices[0].message.content
            messages.append({'role': 'assistant', 'content': gpt_4v_res})

            # 移除網頁上的框線
            if (not args.text_only) and rects:
                logging.info(f"Num of interactive elements: {len(rects)}")
                for rect_ele in rects:
                    driver_task.execute_script("arguments[0].remove()", rect_ele)
                rects = []

            # === 解析多組 Thought / Action ===
            thought_action_pairs = re.findall(
                r'Thought\s*\d*:\s*(.*?)\s*Action\s*\d*:\s*(.*?)(?=(\nThought|\Z))',
                gpt_4v_res,
                re.DOTALL
            )
            if not thought_action_pairs:
                logging.error("Cannot find multiple Thought/Action pairs in GPT-4 response.")
                fail_obs = "Format ERROR: Your reply should include multiple Thought/Action pairs like: Thought 1:..., Action 1:..., etc."
                continue

            # === 組裝 Orchestrator prompt ===
            orchestration_prompt = SYSTEM_ORCHESTRATION.strip() + "\n\n"
            for i, (thought, action, _) in enumerate(thought_action_pairs):
                orchestration_prompt += f"Thought {i+1}: {thought.strip()}\nAction {i+1}: {action.strip()}\n"
            orchestration_prompt += f"\nScreenshot: [The screenshot is attached]\nTask Goal: {task['ques']}"

            # === 呼叫 Orchestrator Agent (GPT-4v) 來選最好的 Thought Index ===
            orchestrator_messages = [
                {'role': 'system', 'content': SYSTEM_ORCHESTRATION},
                {
                    'role': 'user',
                    'content': [
                        {'type': 'text', 'text': orchestration_prompt},
                        {'type': 'image_url', 'image_url': {"url": f"data:image/png;base64,{b64_img}"}}
                    ]
                }
            ]
            _, _, orchestrator_error, orchestrator_response = call_gpt4v_api(args, client, orchestrator_messages)
            if orchestrator_error:
                fail_obs = "Orchestration failed. Please retry."
                continue

            # === 擷取 Orchestrator 的決策 ===
            match = re.search(r"Thought Index\s*:\s*(\d+)", orchestrator_response.choices[0].message.content)
            if not match:
                fail_obs = "Failed to parse Orchestrator's decision. Please ensure format like: Thought Index:1"
                continue

            selected_idx = int(match.group(1)) - 1
            bot_thought = thought_action_pairs[selected_idx][0].strip()
            chosen_action = thought_action_pairs[selected_idx][1].strip()

            # 顯示與記錄行為
            trajectory_info = f"Thought {bot_thought}\nAction {chosen_action}"
            error_info = f"Error: {error_exist}\nExplanation: {EGA_explanation}"
            if args.trajectory:
                current_history += trajectory_info
                if activate_EGA:
                    current_history += error_info

            print(f"Step {it}:\n{error_info}\n{trajectory_info}\n----")

            # 提取動作資訊
            action_key, info = extract_information(chosen_action)


            # 儲存每一輪 WebAgent 操作
            answer_content = None
            if action_key == "answer":
                answer_content = info.get('content', '')

            orchestration_record = {
                "iteration": it,
                "candidates": [
                    {
                        "thought": pair[0].strip(),
                        "action": pair[1].strip()
                    } for pair in thought_action_pairs
                ],
                "selected_index": selected_idx,
                "selected_thought": bot_thought,
                "selected_action": chosen_action
            }

            orchestrator_logs.append(orchestration_record)

            log_entry = {
                "iteration": it,
                "agent": "WebAgent",
                "thought": bot_thought,
                "action": chosen_action,
                "action_type": action_key,
                "answer": answer_content,
                "error_detected": error_exist,
                "ega_explanation": EGA_explanation if error_exist else ""
            }

            log_records.append(log_entry)

            fail_obs = ""
            pdf_obs = ""
            warn_obs = ""
            # execute action
            try:
                window_handle_task = driver_task.current_window_handle
                driver_task.switch_to.window(window_handle_task)

                if action_key == 'click':
                    if not args.text_only:
                        click_ele_number = int(info[0])
                        web_ele = web_eles[click_ele_number]
                    else:
                        click_ele_number = info[0]
                        element_box = obs_info[click_ele_number]['union_bound']
                        element_box_center = (element_box[0] + element_box[2] // 2,
                                            element_box[1] + element_box[3] // 2)
                        web_ele = driver_task.execute_script("return document.elementFromPoint(arguments[0], arguments[1]);", element_box_center[0], element_box_center[1])

                    ele_tag_name = web_ele.tag_name.lower()
                    ele_type = web_ele.get_attribute("type")

                    exec_action_click(info, web_ele, driver_task)

                    # deal with PDF file
                    current_files = sorted(os.listdir(args.download_dir))
                    if current_files != download_files:
                        # wait for download finish
                        time.sleep(10)
                        current_files = sorted(os.listdir(args.download_dir))

                        current_download_file = [pdf_file for pdf_file in current_files if pdf_file not in download_files and pdf_file.endswith('.pdf')]
                        if current_download_file:
                            pdf_file = current_download_file[0]
                            pdf_obs = get_pdf_retrieval_ans_from_assistant(client, os.path.join(args.download_dir, pdf_file), task['ques'])
                            shutil.copy(os.path.join(args.download_dir, pdf_file), task_dir)
                            pdf_obs = "You downloaded a PDF file, I ask the Assistant API to answer the task based on the PDF file and get the following response: " + pdf_obs
                        download_files = current_files

                    if ele_tag_name == 'button' and ele_type == 'submit':
                        time.sleep(10)

                elif action_key == 'wait':
                    time.sleep(5)

                elif action_key == 'type':
                    if not args.text_only:
                        type_ele_number = int(info['number'])
                        web_ele = web_eles[type_ele_number]
                    else:
                        type_ele_number = info['number']
                        element_box = obs_info[type_ele_number]['union_bound']
                        element_box_center = (element_box[0] + element_box[2] // 2,
                                            element_box[1] + element_box[3] // 2)
                        web_ele = driver_task.execute_script("return document.elementFromPoint(arguments[0], arguments[1]);", element_box_center[0], element_box_center[1])

                    warn_obs = exec_action_type(info, web_ele, driver_task)
                    if 'wolfram' in task['web']:
                        time.sleep(5)

                elif action_key == 'scroll':
                    if not args.text_only:
                        exec_action_scroll(info, web_eles, driver_task, args, None)
                    else:
                        exec_action_scroll(info, None, driver_task, args, obs_info)

                elif action_key == 'goback':
                    driver_task.back()
                    time.sleep(2)

                elif action_key == 'google':
                    driver_task.get('https://www.google.com/')
                    time.sleep(2)

                # 這個代表結束
                elif action_key == 'answer':
                    logging.info(info['content'])
                    logging.info('finish!!')
                    break

                else:
                    raise NotImplementedError
                fail_obs = ""
            except Exception as e:
                logging.error('driver error info:')
                logging.error(e)
                if 'element click intercepted' not in str(e):
                    fail_obs = "The action you have chosen cannot be executed. Please double-check if you have selected the wrong Numerical Label or Action or Action format. Then provide the revised Thought and Action."
                else:
                    fail_obs = ""
                time.sleep(2)


        # print_message(messages, task_dir)
        driver_task.quit()
        # === 寫入整份 JSON 紀錄檔 ===
        log_path = os.path.join(task_dir, 'agent_log.json')
        with open(log_path, 'w', encoding='utf-8') as f:
            json.dump({
                "webagent_steps": log_records,
                "orchestration": orchestrator_logs
            }, f, ensure_ascii=False, indent=2)

        logging.info(f'Total cost: {accumulate_prompt_token / 1000 * 0.01 + accumulate_completion_token / 1000 * 0.03}')


if __name__ == '__main__':
    main()
    print('End of process')
