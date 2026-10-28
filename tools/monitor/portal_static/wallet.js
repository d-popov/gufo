'use strict';

// USDC top-ups from browser wallets.
//  Solana: Wallet Standard (Phantom, Solflare, Brave Wallet, ...). Link = sign the
//    server's challenge text; pay = the server builds an unsigned TransferChecked
//    transaction, the wallet signs and sends it, the server checks it on chain.
//  Base: EIP-1193 provider (MetaMask preferred via EIP-6963). Link = personal_sign;
//    pay = eth_sendTransaction of an ERC-20 transfer to the USDC contract.
// Deposits are credited only when the paying wallet is linked to the account.

window.GufoWallet = (() => {
  const SOLANA_CHAIN = 'solana:mainnet';
  const SOLANA_FEATURES = ['standard:connect', 'solana:signMessage', 'solana:signAndSendTransaction'];
  const KNOWN_SOLANA = /phantom|solflare|brave/i;
  const BASE_CHAIN_ID = '0x2105';  // 8453
  const BASE_CHAIN = {
    chainId: BASE_CHAIN_ID,
    chainName: 'Base',
    nativeCurrency: { name: 'Ether', symbol: 'ETH', decimals: 18 },
    rpcUrls: ['https://mainnet.base.org'],
    blockExplorerUrls: ['https://basescan.org'],
  };
  const MIN_MICRO = 1_000_000;
  const MAX_MICRO = 1_000_000_000;
  const POLL_LIMIT_MS = 30 * 60 * 1000;
  const PENDING_KEY = 'gufo-pending-deposits';

  let ctx = null;  // { api, reload, fmtUsd, parseUsd, el } from portal.js
  let me = null;
  let busy = false;
  let solana = null;  // { wallet, account }
  let base = null;    // { entry: { info, provider }, address }
  const polling = new Set();
  const $ = (id) => document.getElementById(id);

  // -------------------------------------------------------------- encoding

  const ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';

  function b58encode(bytes) {
    let zeros = 0;
    while (zeros < bytes.length && bytes[zeros] === 0) zeros += 1;
    const digits = [];  // base-58 digits, least significant first
    for (let i = zeros; i < bytes.length; i += 1) {
      let carry = bytes[i];
      for (let j = 0; j < digits.length; j += 1) {
        carry += digits[j] * 256;
        digits[j] = carry % 58;
        carry = Math.floor(carry / 58);
      }
      while (carry > 0) {
        digits.push(carry % 58);
        carry = Math.floor(carry / 58);
      }
    }
    return '1'.repeat(zeros) + digits.reverse().map((d) => ALPHABET[d]).join('');
  }

  function base64ToBytes(text) {
    return Uint8Array.from(atob(text), (c) => c.charCodeAt(0));
  }

  function utf8Hex(text) {
    return `0x${[...new TextEncoder().encode(text)].map((b) => b.toString(16).padStart(2, '0')).join('')}`;
  }

  const isEvmAddress = (value) => /^0x[0-9a-fA-F]{40}$/.test(value ?? '');

  // ERC-20 transfer(address,uint256) calldata.
  function erc20Transfer(to, amount) {
    if (!isEvmAddress(to)) throw new Error('The server sent an invalid receiver address.');
    const word = (hex) => hex.padStart(64, '0');
    return `0xa9059cbb${word(to.slice(2).toLowerCase())}${word(BigInt(amount).toString(16))}`;
  }

  // -------------------------------------------------------------- discovery

  // Wallet Standard: wallets register themselves through these two events,
  // whichever of app and wallet loads first.
  const solanaWallets = [];
  const standardApi = Object.freeze({
    register(...wallets) {
      const added = wallets.filter((w) => isSolanaWallet(w) && !solanaWallets.includes(w));
      solanaWallets.push(...added);
      if (added.length) render();
      return () => {
        for (const w of added) {
          const index = solanaWallets.indexOf(w);
          if (index >= 0) solanaWallets.splice(index, 1);
        }
        render();
      };
    },
  });

  function isSolanaWallet(wallet) {
    return Array.isArray(wallet?.chains) && wallet.chains.some((c) => String(c).startsWith('solana:'))
      && SOLANA_FEATURES.every((f) => wallet.features?.[f]);
  }

  window.addEventListener('wallet-standard:register-wallet', (event) => {
    try {
      event.detail(standardApi);
    } catch (error) {
      console.error('wallet registration failed', error);
    }
  });
  window.dispatchEvent(new CustomEvent('wallet-standard:app-ready', { detail: standardApi }));

  // EIP-6963: every injected EVM wallet announces itself.
  const evmProviders = new Map();
  window.addEventListener('eip6963:announceProvider', (event) => {
    const detail = event.detail;
    if (detail?.info?.uuid && typeof detail.provider?.request === 'function') {
      evmProviders.set(detail.info.uuid, detail);
      render();
    }
  });
  window.dispatchEvent(new Event('eip6963:requestProvider'));
  window.addEventListener('ethereum#initialized', () => render());

  function baseProviders() {
    const announced = [...evmProviders.values()];
    if (announced.length) {
      const rank = (e) => (e.info.rdns === 'io.metamask' ? 0 : /metamask/i.test(e.info.name ?? '') ? 1 : 2);
      return announced.sort((a, b) => rank(a) - rank(b));
    }
    const injected = window.ethereum;
    if (!injected || typeof injected.request !== 'function') return [];
    const many = Array.isArray(injected.providers) ? injected.providers : [injected];
    const provider = many.find((p) => p.isMetaMask) ?? injected;
    const name = provider.isMetaMask ? 'MetaMask' : 'Browser wallet';
    return [{ info: { uuid: 'injected', name }, provider }];
  }

  // -------------------------------------------------------------- status

  function setStatus(text, kind = 'info') {
    const node = $('topup-status');
    node.textContent = text ?? '';
    node.className = `topup-status${kind === 'info' ? '' : ` ${kind}`}`;
    node.hidden = !text;
  }

  function walletError(error) {
    const code = error?.code ?? error?.data?.originalError?.code;
    if (code === 4001 || (!error?.status && /user (rejected|denied|cancel)/i.test(error?.message ?? ''))) {
      return 'You cancelled the request in the wallet.';
    }
    if (code === -32002) return 'The wallet already has a pending request: open it and finish or cancel it.';
    return error?.message ? String(error.message) : String(error);
  }

  async function run(task) {
    if (busy) return;
    busy = true;
    render();
    try {
      await task();
    } catch (error) {
      setStatus(walletError(error), 'error');
    } finally {
      busy = false;
      render();
    }
  }

  function isLinked(chain, address) {
    const norm = (a) => (chain === 'base' ? String(a).toLowerCase() : a);
    return (me?.wallets ?? []).some((w) => w.chain === chain && norm(w.address) === norm(address));
  }

  function readAmount() {
    const micro = ctx.parseUsd($('topup-amount').value);
    if (micro == null || micro < MIN_MICRO || micro > MAX_MICRO) {
      throw new Error('Enter an amount between $1 and $1,000 (up to 6 decimals).');
    }
    return micro;
  }

  async function linkWith(chain, address, sign) {
    setStatus('Sign the message in your wallet to prove you own it (this costs nothing).');
    const { message } = await ctx.api('POST', 'wallet/challenge', { chain, address });
    const signature = await sign(message);
    await ctx.api('POST', 'wallet/link', { chain, address, signature });
    await ctx.reload();
    setStatus(`Wallet ${address} is linked to your account.`, 'done');
  }

  // -------------------------------------------------------------- Solana

  async function connectSolana(wallet) {
    setStatus(`Connecting to ${wallet.name}…`);
    const result = await wallet.features['standard:connect'].connect();
    const accounts = result?.accounts?.length ? result.accounts : wallet.accounts ?? [];
    const account = accounts.find((a) => !a.chains || a.chains.some((c) => String(c).startsWith('solana:')));
    if (!account) throw new Error(`${wallet.name} did not share a Solana account.`);
    solana = { wallet, account };
    setStatus(isLinked('solana', account.address) ? null
      : 'Connected. Link the wallet before paying with it.');
  }

  async function linkSolana() {
    const { wallet, account } = solana;
    await linkWith('solana', account.address, async (message) => {
      const [output] = await wallet.features['solana:signMessage'].signMessage(
        { account, message: new TextEncoder().encode(message) });
      if (output?.signature?.length !== 64) throw new Error('The wallet returned no valid signature.');
      return b58encode(output.signature);
    });
  }

  async function paySolana() {
    const { wallet, account } = solana;
    if (!isLinked('solana', account.address)) throw new Error('Link this wallet first.');
    const amount = readAmount();
    setStatus('Preparing the transaction…');
    const prepared = await ctx.api('POST', 'solana/prepare', { owner: account.address, amount_micro: amount });
    setStatus(`Approve the payment of ${ctx.fmtUsd(amount)} USDC in ${wallet.name}.`);
    const [output] = await wallet.features['solana:signAndSendTransaction'].signAndSendTransaction({
      account, transaction: base64ToBytes(prepared.transaction), chain: SOLANA_CHAIN });
    if (!output?.signature?.length) throw new Error('The wallet did not return a transaction signature.');
    track('solana', b58encode(output.signature));
  }

  // -------------------------------------------------------------- Base

  async function ensureBaseChain(provider) {
    const current = String(await provider.request({ method: 'eth_chainId' })).toLowerCase();
    if (current === BASE_CHAIN_ID) return;
    setStatus('Switch your wallet to the Base network.');
    try {
      await provider.request({ method: 'wallet_switchEthereumChain', params: [{ chainId: BASE_CHAIN_ID }] });
    } catch (error) {
      const code = error?.code ?? error?.data?.originalError?.code;
      if (code !== 4902) throw error;
      setStatus('Add the Base network to your wallet.');
      await provider.request({ method: 'wallet_addEthereumChain', params: [BASE_CHAIN] });
    }
    const now = String(await provider.request({ method: 'eth_chainId' })).toLowerCase();
    if (now !== BASE_CHAIN_ID) throw new Error('The wallet is not on the Base network.');
  }

  async function connectBase(entry) {
    setStatus(`Connecting to ${entry.info.name}…`);
    const accounts = await entry.provider.request({ method: 'eth_requestAccounts' });
    const address = accounts?.[0];
    if (!isEvmAddress(address)) throw new Error(`${entry.info.name} did not share an account.`);
    await ensureBaseChain(entry.provider);
    base = { entry, address: address.toLowerCase() };
    setStatus(isLinked('base', base.address) ? null : 'Connected. Link the wallet before paying with it.');
  }

  async function linkBase() {
    const { entry, address } = base;
    await linkWith('base', address, (message) =>
      entry.provider.request({ method: 'personal_sign', params: [utf8Hex(message), address] }));
  }

  async function payBase() {
    const { entry, address } = base;
    if (!isLinked('base', address)) throw new Error('Link this wallet first.');
    const amount = readAmount();
    const chain = me.chains.base;
    if (!isEvmAddress(chain.usdc)) throw new Error('The server sent an invalid USDC contract address.');
    await ensureBaseChain(entry.provider);
    setStatus(`Approve the transfer of ${ctx.fmtUsd(amount)} USDC in ${entry.info.name}.`);
    const hash = await entry.provider.request({
      method: 'eth_sendTransaction',
      params: [{ from: address, to: chain.usdc, value: '0x0', data: erc20Transfer(chain.receiver, amount) }],
    });
    if (!/^0x[0-9a-fA-F]{64}$/.test(hash ?? '')) throw new Error('The wallet did not return a transaction hash.');
    track('base', hash);
  }

  // -------------------------------------------------------------- deposits

  const REJECT_TEXT = {
    sender_not_linked: 'The paying wallet is not linked to this account, so the payment was not credited. '
      + 'Contact the admin with the transaction id.',
  };

  function pendingList() {
    try {
      return JSON.parse(sessionStorage.getItem(PENDING_KEY) ?? '[]');
    } catch {
      return [];
    }
  }

  function savePending(list) {
    try {
      sessionStorage.setItem(PENDING_KEY, JSON.stringify(list));
    } catch {
      // storage disabled: polling still runs for this page
    }
  }

  const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

  // Polls until credited or rejected. The server keeps re-checking pending
  // deposits on its own, so giving up here never loses a payment.
  async function trackDeposit(chain, tx, started = Date.now()) {
    const id = `${chain}:${tx}`;
    if (polling.has(id)) return;
    polling.add(id);
    if (!pendingList().some((p) => p.chain === chain && p.tx === tx)) {
      savePending([...pendingList(), { chain, tx, started }]);
    }
    const confirmations = me?.chains?.base?.confirmations ?? 12;
    const waiting = chain === 'base'
      ? `waiting for ${confirmations} confirmations on Base (about ${Math.max(1, Math.ceil((confirmations * 2) / 60))} min)`
      : 'waiting for confirmations on Solana (usually under a minute)';
    try {
      while (Date.now() - started < POLL_LIMIT_MS) {
        let result;
        try {
          result = await ctx.api('POST', 'deposit', { chain, tx });
        } catch (error) {
          if (error.status && error.status < 500 && error.status !== 429) throw error;
          result = { status: 'pending', reason: error.message };
        }
        if (result.status === 'credited') {
          setStatus(`Payment confirmed: ${ctx.fmtUsd(result.amount_micro)} added to your balance.`, 'done');
          await ctx.reload().catch(() => {});
          return;
        }
        if (result.status === 'rejected') {
          throw new Error(REJECT_TEXT[result.reason]
            ?? `The payment was not credited: ${result.reason || 'rejected by the server'}.`);
        }
        setStatus(`Payment sent (${tx}), ${waiting}…`);
        await sleep(chain === 'base' ? 8000 : 4000);
      }
      setStatus(`Payment ${tx} is still waiting for confirmations. It is credited automatically once `
        + 'confirmed; check History later.');
    } finally {
      polling.delete(id);
      savePending(pendingList().filter((p) => !(p.chain === chain && p.tx === tx)));
    }
  }

  // Polling runs in the background so the buttons stay usable meanwhile.
  function track(chain, tx, started) {
    trackDeposit(chain, tx, started).catch((error) => setStatus(walletError(error), 'error'));
  }

  // -------------------------------------------------------------- rendering

  function walletButton(name, icon, onClick) {
    const button = ctx.el('button', { type: 'button' });
    if (typeof icon === 'string' && /^data:image\/(png|svg\+xml|webp|gif|jpeg)[;,]/.test(icon)) {
      button.append(ctx.el('img', { src: icon, alt: '' }));
    }
    button.append(name);
    button.disabled = busy;
    button.addEventListener('click', () => run(onClick));
    return button;
  }

  function renderConnected(chain, connected, address) {
    $(`${chain}-connected`).hidden = !connected;
    if (!connected) return;
    const linked = isLinked(chain, address);
    $(`${chain}-address`).textContent = address;
    $(`${chain}-linked`).textContent = linked ? 'linked' : 'not linked yet';
    $(`${chain}-linked`).className = linked ? 'ok' : 'bad';
    $(`${chain}-link`).hidden = linked;
    $(`${chain}-link`).disabled = busy;
    $(`${chain}-pay`).disabled = busy || !linked;
  }

  function render() {
    if (!ctx || !me) return;
    const approved = me.status === 'approved';
    $('topup-locked').hidden = approved;
    $('topup-body').hidden = !approved;
    const chains = me.chains ?? {};

    $('chain-solana').hidden = !chains.solana;
    const sorted = [...solanaWallets].sort((a, b) =>
      Number(!KNOWN_SOLANA.test(a.name)) - Number(!KNOWN_SOLANA.test(b.name)));
    $('solana-wallets').replaceChildren(...(sorted.length
      ? sorted.map((w) => walletButton(w.name, w.icon, () => connectSolana(w)))
      : [ctx.el('p', { class: 'muted' },
        'No Solana wallet found. Install Phantom or Solflare, or enable Brave Wallet, then reload.')]));
    renderConnected('solana', solana, solana?.account.address);

    $('chain-base').hidden = !chains.base;
    const providers = baseProviders();
    $('base-wallets').replaceChildren(...(providers.length
      ? providers.map((entry) => walletButton(entry.info.name, entry.info.icon, () => connectBase(entry)))
      : [ctx.el('p', { class: 'muted' }, 'No Ethereum wallet found. Install MetaMask, then reload.')]));
    renderConnected('base', base, base?.address);

    const wallets = me.wallets ?? [];
    $('wallets').replaceChildren(...(wallets.length
      ? wallets.map((w) => ctx.el('li', {}, `${w.chain === 'base' ? 'Base' : 'Solana'}: ${w.address}`))
      : [ctx.el('li', { class: 'muted' }, 'None yet.')]));
  }

  function init(context) {
    ctx = context;
    $('topup-form').addEventListener('submit', (event) => event.preventDefault());
    $('solana-link').addEventListener('click', () => run(linkSolana));
    $('solana-pay').addEventListener('click', () => run(paySolana));
    $('base-link').addEventListener('click', () => run(linkBase));
    $('base-pay').addEventListener('click', () => run(payBase));
  }

  return {
    init,
    render(account) {
      const first = me === null;
      me = account;
      render();
      if (first && me.status === 'approved') {
        for (const p of pendingList()) track(p.chain, p.tx, p.started);
      }
    },
    b58encode,
    erc20Transfer,
  };
})();
