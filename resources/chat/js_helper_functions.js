function getElementById(id) {
    return document.getElementById(id);
}

function getElement(query) {
    return document.querySelector(query);
}

function fix_index() {
    insert = getElement('#insert')
    if (insert) {
        return
    }
    console.log('Adding insert')
    let messages = document.querySelectorAll('[id^=message-]')
    let last_message = [...messages].pop()
    insert = document.createElement('span');
    insert.setAttribute('id', 'insert');
    let smooth_operator = document.querySelectorAll('[class^=x-wrap]').length !== 0;
    if (!smooth_operator && last_message.classList.contains('consecutive')) {
        last_message.after(insert);
    } else {
        last_message.append(insert);
    }
}

function removeElement(query) {
    let elem = getElement(query);
    if (elem) {
        // Check for consecutive messages
        let elem_messages = elem.querySelectorAll('[id^=message-]');
        if (elem_messages.length !== 0) {
            let child_messages = Array.from(elem_messages)
            let first_message = child_messages.shift();

            let smooth_operator = elem.querySelector('[class^=x-wrap]');
            if (smooth_operator) {
                first_message.classList.remove('consecutive');
                first_message.append(...child_messages)
                elem.replaceWith(first_message);
            } else {
                // Stockholm theme
                let elem_replace = elem.querySelector('[class^=x-message]');
                if (elem_replace) {
                    elem.setAttribute('id', first_message.getAttribute('id'))
                    let time = elem.querySelector('[class^=x-time]')
                    if (time) {
                        time.replaceWith(first_message.querySelector('[class^=x-time]'));
                    }
                    elem_replace.replaceChildren(...first_message.children);
                    first_message.remove();
                }
                elem.append(...child_messages);
            }
        } else {
            elem.remove();
        }

        // Return if we're removing #insert
        if (query.startsWith('#insert')) {
            return;
        }

        // Check if insert element is still in the chat. Could be gone if we removed last message
        fix_index();
    }
}

function parseHtml(html) {
    let template = document.createElement('template');
    template.innerHTML = html;
    return template.content;
}

function replaceElement(query, content) {
    let element = getElement(query);
    if (element) {
        content = parseHtml(content);
        element.replaceWith(content);
    }
}

function appendElement(query, content) {
    let element = getElement(query);
    if (element) {
        content = parseHtml(content);
        element.append(content);
    }
}

function emptyElement(query) {
    let elem = getElement(query);
    if (elem) {
        while (elem.firstChild) {
            elem.firstChild.remove();
        }
    }
}

function previousSibling(id, content) {
    let elem = getElementById(id);
    if (elem) {
        content = parseHtml(content);
        elem.previousElementSibling.append(content);
    }
}

function insertAsParent(id, content, new_consecutive) {
    let elem = getElementById(id);
    if (elem) {
        content = parseHtml(content);
        new_consecutive = parseHtml(new_consecutive);
        let elem_messages = elem.querySelectorAll('[id^=message-]');
        let insert = content.querySelector('[id^=insert]');

        if (elem_messages.length !== 0) {
            let child_messages = Array.from(elem_messages);
            child_messages.unshift(new_consecutive);
            console.log(child_messages);
            insert.replaceWith(child_messages);
            elem.replaceWith(content);
            fix_index();
            return
        }
        insert.replaceWith(new_consecutive);
        elem.replaceWith(content);
        fix_index();
    }
}

function appendOutside(id, content) {
    let elem = getElementById(id);
    if (elem) {
        content = parseHtml(content);
        elem.after(content);
    }
}

function prependOutside(id, content) {
    let elem = getElementById(id);
    if (elem) {
        content = parseHtml(content);
        elem.before(content);
    }
}

function updateElement(query, content) {
    let elem = getElement(query);
    if (elem) {
        while (elem.firstChild) {
            elem.firstChild.remove();
        }
        content = parseHtml(content);
        elem.append(content);
    }
}

function addContextMenuToElement(query) {
    let elem = getElement(query);
    if (elem) {
        elem.addEventListener('contextmenu', handleContextMenu);
    }
}

function appendMessageToChat(content) {
    removeElement('#insert');
    appendElement('#chat', content);
}

function styleElement(query, property, value) {
    let elem = getElement(query);
    if (elem) {
        elem.style.setProperty(property, value);
    }
}

function getHeightElement(query) {
    let elem = getElement(query);
    if (elem) {
        return elem.offsetHeight;
    }
    return 0;
}

function scrollToBottom() {
    setTimeout(window.scrollTo(0, document.body.scrollHeight), 5);
}

function print(content) {
    console.error(content);
}

function handleContextMenu(e) {
    e.stopPropagation();
    let id = e.target.getAttribute('id');
    if (id === null) {
        id = e.target.parentElement.getAttribute('id')
    }
    if (id === null) {
        id = e.target.parentElement.parentElement.getAttribute('id')
    }
    if (id === null) {
        id = e.target.closest('[id^=message-]').getAttribute('id');
    }
    chat.handleContextMenuEvent(id);
}

window.onload = function() {
    setTimeout(function() {
        window.chat = chat;
        chat._JH_LoadFinished(true);
    }, 1000);
}
