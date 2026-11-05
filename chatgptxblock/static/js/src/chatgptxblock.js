/* Javascript for ChatgptXBlock. */
function ChatgptXBlock(runtime, element) {
  // Get elements
  var questionInput = $('#question-input', element);
  var questionForm = $('#question-form', element);
  var submitBtn = $('#submit-btn', element);
  var chatMessages = $('#chat-messages', element);
  var errorMessage = $('#error-message', element);

  // Handler URLs
  var getAnswerUrl = runtime.handlerUrl(element, 'get_answer');
  var getConversationUrl = runtime.handlerUrl(element, 'get_conversation');

  // (8) Helper function to show errors in the UI
  function showError(msg) {
      errorMessage.text(msg);
  }

  // Submit button is active only when the input has text
  function updateSubmitState() {
      var hasText = !!(questionInput.val() && questionInput.val().trim());
      submitBtn.prop('disabled', !hasText);
  }
  questionInput.on('input', updateSubmitState);
  updateSubmitState();

  // Auto-grow the textarea while typing (capped by CSS max-height),
  // reset after each send.
  function resizeQuestionInput() {
      questionInput.css('height', 'auto');
      questionInput.css('height', questionInput[0].scrollHeight + 'px');
  }
  questionInput.on('input', resizeQuestionInput);

  // Render one message bubble in the transcript (role: 'user' or 'assistant')
  function appendMessage(role, content) {
      // Drop the empty-state placeholder once real messages exist
      if (role === 'user' || role === 'assistant') {
          chatMessages.children('.chatgpt__message--empty').remove();
      }
      var bubble = $('<div></div>')
          .addClass('chatgpt__message chatgpt__message--' + role);
      if (role === 'assistant') {
          // Render the model's markdown; sanitize before inserting HTML.
          bubble.html(DOMPurify.sanitize(marked.parse(content)));
      } else {
          // Keep student text plain, preserve newlines.
          bubble.text(content);
      }
      chatMessages.append(bubble);
      chatMessages.scrollTop(chatMessages[0].scrollHeight);
  }

  // (2) Submit question for conversation (button click, Enter key, or form submit)
  var awaitingAnswer = false;
  function askQuestion() {
      var question = questionInput.val();
      if (!question || !question.trim()) {
          showError('Please enter a question.');
          return;
      }
      if (awaitingAnswer) {
          return; // one question at a time
      }
      awaitingAnswer = true;
      submitBtn.prop('disabled', true);

      // Show the student's message immediately, then a loading bubble.
      showError('');
      chatMessages.children('.chatgpt__message--empty').remove();
      questionInput.val('');
      resizeQuestionInput();
      appendMessage('user', question);
      var loadingBubble = $('<div></div>')
          .addClass('chatgpt__message chatgpt__message--assistant chatgpt__message--loading')
          .text('Thinking...');
      chatMessages.append(loadingBubble);
      chatMessages.scrollTop(chatMessages[0].scrollHeight);

      $.ajax({
          type: "POST",
          url: getAnswerUrl,
          data: JSON.stringify({ question: question }),
          success: function(response) {
              loadingBubble.remove();
              if (response.answer) {
                  appendMessage('assistant', response.answer);
              } else {
                  showError('No answer received.');
              }
          },
          error: function() {
              loadingBubble.remove();
              showError('Error sending question to server.');
          },
          complete: function() {
              awaitingAnswer = false;
              updateSubmitState();
              questionInput.focus();
          }
      });
  }

  // Load the existing session transcript on page load
  $.ajax({
      type: "POST",
      url: getConversationUrl,
      data: JSON.stringify({}),
      success: function(response) {
          (response.messages || []).forEach(function (message) {
              appendMessage(message.role, message.content);
          });
          if (!(response.messages || []).length) {
              chatMessages.append(
                  $('<div></div>')
                      .addClass('chatgpt__message chatgpt__message--empty')
                      .text('No messages yet. Ask your first question below.')
              );
          }
      }
  });

  // Form submit: fires on button click and on Enter inside the textarea
  questionForm.submit(function (event) {
      event.preventDefault();
      askQuestion();
  });

  // Enter sends the question, Shift+Enter inserts a new line
  questionInput.keydown(function (event) {
      if (event.key === 'Enter' && !event.shiftKey) {
          event.preventDefault();
          askQuestion();
      }
  });

  // Focus the input on load so the student can type right away
  questionInput.focus();
}
