(() => {
  const emailOk = v => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v);
  const passwordOk = v => /^[A-Za-z0-9!@#$]{6,20}$/.test(v);
  const phoneOk = v => /^[0-9-]{8,20}$/.test(v);
  const employeeOk = v => /^\d{1,10}$/.test(v) && Number(v) <= 2147483647;
  let csrf = '';
  const state = { verified: false, email: '', employeeAvailable: false };

  const byId = id => document.getElementById(id);
  const text = (element, value = '', type = '') => {
    element.textContent = value;
    element.className = element.className.replace(/\b(error|success)\b/g, '').trim();
    if (type) element.classList.add(type);
  };

  async function refreshCsrf() {
    const response = await fetch('/api/auth/csrf', { credentials: 'same-origin' });
    const data = await response.json();
    if (!response.ok || !data.csrf_token) throw new Error('보안 토큰을 준비하지 못했습니다. 페이지를 새로고침해 주세요.');
    csrf = data.csrf_token;
  }

  async function post(url, payload) {
    if (!csrf) await refreshCsrf();
    const response = await fetch(url, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) throw new Error(data.detail || '요청을 처리하지 못했습니다.');
    return data;
  }

  function init() {
    const loginMessage = byId('loginMessage');
    const signupMessage = byId('signupMessage');
    const email = byId('signupEmail');
    const password = byId('signupPassword');
    const confirm = byId('signupPasswordConfirm');
    const employee = byId('employeeNumber');
    const employeeHint = byId('employeeHint');
    const submit = byId('signupSubmit');
    const protectedFields = byId('protectedFields');

    const canSubmit = () => state.verified && state.email === email.value.trim().toLowerCase() && passwordOk(password.value) && password.value === confirm.value && byId('signupName').value.trim() && phoneOk(byId('signupPhone').value.trim()) && employeeOk(employee.value.trim()) && state.employeeAvailable;
    const sync = () => { submit.disabled = !canSubmit(); };
    const reset = () => {
      state.verified = false; state.email = ''; state.employeeAvailable = false;
      byId('verifyRow').hidden = true; protectedFields.disabled = false;
      byId('verifiedEmailLabel').hidden = true; byId('verificationCode').value = '';
      text(employeeHint); sync();
    };

    byId('openSignupBtn').onclick = () => { byId('signupModal').classList.add('open'); byId('signupModal').setAttribute('aria-hidden', 'false'); email.focus(); };
    const close = () => { byId('signupModal').classList.remove('open'); byId('signupModal').setAttribute('aria-hidden', 'true'); };
    byId('closeSignupBtn').onclick = close;
    byId('signupModal').onclick = event => { if (event.target.matches('[data-close-signup]')) close(); };
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && byId('signupModal').classList.contains('open')) close();
    });
    email.oninput = reset;

    byId('sendEmailBtn').onclick = async () => {
      const value = email.value.trim().toLowerCase();
      if (!emailOk(value)) return text(signupMessage, '올바른 이메일 주소를 입력해 주세요.', 'error');
      const button = byId('sendEmailBtn'); button.disabled = true;
      try {
        const result = await post('/api/auth/email/send', { email: value });
        byId('verifyRow').hidden = false;
        text(signupMessage, `인증번호를 발송했습니다. ${Math.ceil(result.expires_in_sec / 60)}분 안에 입력해 주세요.`, 'success');
        byId('verificationCode').focus();
      } catch (error) { text(signupMessage, error.message, 'error'); }
      finally { button.disabled = false; }
    };

    byId('verifyEmailBtn').onclick = async () => {
      const value = email.value.trim().toLowerCase();
      const code = byId('verificationCode').value.trim();
      if (!/^\d{6}$/.test(code)) return text(signupMessage, '6자리 인증번호를 입력해 주세요.', 'error');
      try {
        await post('/api/auth/email/verify', { email: value, code });
        state.verified = true; state.email = value; protectedFields.disabled = false;
        byId('verifiedEmailLabel').hidden = false; byId('verifiedEmailLabel').textContent = `인증된 로그인 ID: ${value}`;
        text(signupMessage, '이메일 인증이 완료되었습니다. 비밀번호와 가입 정보를 입력해 주세요.', 'success');
        password.focus(); sync();
      } catch (error) { text(signupMessage, error.message, 'error'); }
    };

    let employeeTimer;
    employee.oninput = () => {
      state.employeeAvailable = false; clearTimeout(employeeTimer);
      if (!employeeOk(employee.value.trim())) { text(employeeHint, '사번은 최대 10자리 숫자로 입력해 주세요.', 'error'); return sync(); }
      employeeTimer = setTimeout(async () => {
        try {
          const result = await post('/api/auth/availability', { field: 'employee_number', value: employee.value.trim() });
          state.employeeAvailable = result.available;
          text(employeeHint, result.available ? '사용 가능합니다.' : '이미 사용 중입니다.', result.available ? 'success' : 'error');
        } catch (error) { text(employeeHint, error.message, 'error'); }
        sync();
      }, 350);
      sync();
    };
    [password, confirm, byId('signupName'), byId('signupPhone')].forEach(input => input.oninput = () => {
      if (confirm.value) text(byId('passwordHint'), password.value === confirm.value ? '비밀번호가 일치합니다.' : '비밀번호가 일치하지 않습니다.', password.value === confirm.value ? 'success' : 'error');
      sync();
    });

    byId('signupForm').onsubmit = async event => {
      event.preventDefault();
      if (!canSubmit()) return text(signupMessage, '필수 정보를 올바르게 입력해 주세요.', 'error');
      submit.disabled = true;
      try {
        await post('/api/auth/register', { email: state.email, password: password.value, password_confirm: confirm.value, name: byId('signupName').value.trim(), phone_number: byId('signupPhone').value.trim(), employee_number: employee.value.trim() });
        text(signupMessage, '회원가입이 완료되었습니다. 이메일과 비밀번호로 로그인해 주세요.', 'success');
        setTimeout(() => { close(); byId('loginId').focus(); }, 800);
      } catch (error) { text(signupMessage, error.message, 'error'); sync(); }
    };

    byId('loginForm').onsubmit = async event => {
      event.preventDefault();
      const loginEmail = byId('loginId').value.trim().toLowerCase();
      const loginPassword = byId('loginPassword').value;
      if (!emailOk(loginEmail) || !loginPassword) return text(loginMessage, '이메일과 비밀번호를 입력해 주세요.', 'error');
      byId('loginSubmit').disabled = true;
      try {
        const result = await post('/api/auth/login', { login_id: loginEmail, password: loginPassword });
        window.location.assign(result.redirect_url);
      } catch (error) { text(loginMessage, error.message, 'error'); byId('loginSubmit').disabled = false; }
    };
    refreshCsrf().catch(error => text(loginMessage, error.message, 'error'));
  }

  document.addEventListener('DOMContentLoaded', init);
})();
