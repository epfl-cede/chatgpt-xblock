/* Javascript for ChatgptXBlock. */
function ChatgptXBlock(runtime, element) {
  // Get elements
  var questionInput = $('#question-input', element);
  var questionForm = $('#question-form', element);
  var submitBtn = $('#submit-btn', element);
  var chatMessages = $('#chat-messages', element);
  var errorMessage = $('#error-message', element);
  var newConversationBtn = $('#new-conversation-btn', element);
  var newConversationConfirm = $('#new-conversation-confirm', element);
  var confirmResetBtn = $('#confirm-reset-btn', element);
  var cancelResetBtn = $('#cancel-reset-btn', element);

  // Handler URLs
  var getAnswerUrl = runtime.handlerUrl(element, 'get_answer');
  var getConversationUrl = runtime.handlerUrl(element, 'get_conversation');
  var resetConversationUrl = runtime.handlerUrl(element, 'reset_conversation');

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
  updateNewConversationState();

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

  // Render the empty-state placeholder in an empty transcript
  function showEmptyState() {
      chatMessages.append(
          $('<div></div>')
              .addClass('chatgpt__message chatgpt__message--empty')
              .text('No messages yet. Ask your first question below.')
      );
  }

  // 'New conversation' is only meaningful (and only offered) once a history exists
  function updateNewConversationState() {
      var hasHistory = chatMessages.children('.chatgpt__message--user').length > 0;
      newConversationBtn.prop('disabled', !hasHistory || awaitingAnswer);
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
      // Close a pending 'New conversation' confirm: the sent question belongs
      // to the current conversation, and a stale Start button would wipe it.
      showConfirmBar(false);

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
          // 15s client-side timeout: matches the server-side OpenAI timeout.
          // jQuery aborts (kills) the request when it fires; the student can
          // then simply submit the question again.
          timeout: 15000,
          success: function(response) {
              loadingBubble.remove();
              if (response.error) {
                  // Server-side graceful failure (timeout, auth, rate limit...).
                  // The question bubble stays visible so it can be re-sent.
                  showError(response.error);
                  questionInput.val(question);
                  resizeQuestionInput();
                  updateSubmitState();
              } else if (response.answer) {
                  appendMessage('assistant', response.answer);
              } else {
                  showError('No answer received. Please try again.');
              }
          },
          error: function(xhr, textStatus) {
              loadingBubble.remove();
              if (textStatus === 'timeout') {
                  showError('The request timed out after 15 seconds and was cancelled. Please try again.');
              } else if (textStatus === 'abort') {
                  showError('The request was cancelled. Please try again.');
              } else {
                  showError('Error sending question to server. Please try again.');
              }
              // Restore the question so the student can repeat it.
              questionInput.val(question);
              resizeQuestionInput();
              updateSubmitState();
          },
          complete: function() {
              awaitingAnswer = false;
              updateNewConversationState();
              questionInput.focus();
          }
      });
  }

  // --- New conversation ---
  // The transcript stays until the student confirms; then the server clears
  // conversation_history and the UI resets to the empty state.
  function showConfirmBar(show) {
      newConversationConfirm.prop('hidden', !show);
      newConversationBtn.prop('hidden', show);
  }

  newConversationBtn.on('click', function () {
      showConfirmBar(true);
  });

  cancelResetBtn.on('click', function () {
      showConfirmBar(false);
  });

  confirmResetBtn.on('click', function () {
      if (awaitingAnswer) {
          return;
      }
      confirmResetBtn.prop('disabled', true);
      $.ajax({
          type: "POST",
          url: resetConversationUrl,
          data: JSON.stringify({}),
          timeout: 15000,
          success: function (response) {
              showConfirmBar(false);
              if (response.status === 'ok') {
                  chatMessages.empty();
                  showEmptyState();
                  updateNewConversationState();
              } else {
                  showError('Could not start a new conversation. Please try again.');
              }
          },
          error: function () {
              showError('Could not start a new conversation. Please try again.');
          },
          complete: function () {
              confirmResetBtn.prop('disabled', false);
          }
      });
  });

  // Load the existing session transcript on page load
  $.ajax({
      type: "POST",
      url: getConversationUrl,
      data: JSON.stringify({}),
      timeout: 15000,
      success: function(response) {
          (response.messages || []).forEach(function (message) {
              appendMessage(message.role, message.content);
          });
          if (!(response.messages || []).length) {
              showEmptyState();
          }
          updateNewConversationState();
      },
      error: function() {
          showError('Could not load the conversation history. Please reload the page.');
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

  // Deliberately NO focus() on load: in the learning MFE the block renders
  // inside an auto-height iframe, and focusing before the iframe has been
  // resized scrolls the iframe document away from the top (TNL-7094).
}
