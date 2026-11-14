import { fileURLToPath } from 'node:url';
import { AutoSubscribe, defineAgent, llm, voice } from '@livekit/agents';
import { realtime } from '@livekit/agents-plugin-google';
import { LocalVideoTrack, TrackSource, VideoBufferType, VideoFrame, VideoSource } from '@livekit/rtc-node';
import { drawFace, FPS, HEIGHT, WIDTH } from './face.mjs';
import { runAgent } from './chert-rooms.mjs';
import { greetOnBridgeReady } from './greet.mjs';
import { startFrameProbe } from './frame-probe.mjs';
import { startCardReader } from './card-session.mjs';
import { HINT_SPEECH, spokenCardId } from './card-reader.mjs';

// Change these instructions to build your own agent. Keep replies conversational.
const instructions = `You are the TriageX DEMO staffing agent on a FaceTime video call, part of a
hackathon demo. Everything is synthetic: no real hospital, patient or staff data.
The caller sees a simple animated face while you talk.
TriageX coordinates emergency-department surge staffing for a simulated Atlanta hospital network.
Keep each reply to one or two short sentences. Be clear that you are an AI.

Staff cards: a separate card scanner reads the caller's fictional TriageX DEMO staff card from their
camera. You cannot see the video yourself. Only the scanner's messages tell you what a card says:
never guess, invent or repeat a card ID or name that the scanner did not report.
If the caller wants to show a card again or show another card, call scan_staff_card.
Never say a card is valid, verified or accepted, and never say you recorded a confirmation: checking a
card against the roster is not connected yet.`;
// Said once, as soon as FaceTime is ready. Keep it short: it is the first thing the caller hears.
const greeting = 'Greet the caller in one short sentence: say you are the TriageX DEMO staffing agent, an AI, and ask them to hold their staff card up to the camera.';
// Gemini Live via the Gemini API (GOOGLE_API_KEY). Override with GEMINI_LIVE_MODEL in .env.local.
const liveModel = process.env.GEMINI_LIVE_MODEL || 'gemini-2.5-flash-native-audio-preview-12-2025';
// Development aid (CARD_AUTO_RESCAN=1): after a card is read or a scan times out, start a new scan
// a few seconds later, so several cards can be tested in one short call without voice commands.
const autoRescan = process.env.CARD_AUTO_RESCAN === '1';

const agent = defineAgent({
  entry: async ctx => {
    // Register track handlers before connecting, and subscribe to video as well as audio, so the
    // caller's facetime-remote-video track reaches this agent. The frame probe (Stage C evidence)
    // now runs only with FRAME_PROBE=1: the card reader handles the caller video.
    const probe = process.env.FRAME_PROBE === '1' ? startFrameProbe(ctx.room) : null;
    ctx.addShutdownCallback(async () => probe?.stop());

    /** @type {voice.AgentSession | null} */
    let session = null;
    /** @type {string | null} */
    let lastCardId = null;
    let rescanTimer = null;
    const say = text => {
      try { session?.generateReply({ instructions: text }); } catch { /* the call ended */ }
    };
    const reader = startCardReader(ctx.room, {
      onEvent: event => {
        if (event.type === 'nudge') {
          say(HINT_SPEECH[event.hint]);
        } else if (event.type === 'accepted') {
          const r = event.reading;
          lastCardId = r.card_id;
          say(`The card scanner read the caller's card. Tell them, in two short sentences, what it says: ` +
            `ID ${spokenCardId(r.card_id)}` +
            (r.name ? `, name ${r.name}` : '') +
            (r.availability ? `, availability ${r.availability}` : '') +
            `. Read the ID character by character. Then say the card still has to be checked against the TriageX roster, ` +
            `which is not connected in this build, so nothing is verified yet.`);
        } else if (event.type === 'timed_out') {
          say('Tell the caller in one short sentence that you could not read the staff card, and ask whether they would like to try again.');
        }
        if (autoRescan && event.type !== 'nudge') {
          clearTimeout(rescanTimer);
          rescanTimer = setTimeout(() => reader.scan({ ignoreCardId: lastCardId }), 6000);
        }
      },
    });
    ctx.addShutdownCallback(async () => { clearTimeout(rescanTimer); reader.stop(); });

    const scanTool = llm.tool({
      description: 'Start reading a fictional TriageX DEMO staff card from the caller\'s camera. Use it when the caller wants to show a card again or show a different card. The result is reported to you later by the scanner.',
      parameters: {
        type: 'object',
        properties: {
          different_card: { type: 'boolean', description: 'True if the caller is showing a different card from the one just read.' },
        },
        required: [],
      },
      execute: async ({ different_card }) => {
        // In the live test Gemini Live called this tool on its own several times, which restarted
        // scans in progress. A scan that is already running is left alone.
        if (reader.state === 'awaiting') {
          return 'The scanner is already reading the camera. Ask the caller to keep the card 20 to 30 centimetres from the camera and still. Do not state any card details until the scanner reports them.';
        }
        clearTimeout(rescanTimer);
        reader.scan({ ignoreCardId: different_card ? lastCardId : null });
        return 'The scanner is reading the camera now. Ask the caller to hold the card 20 to 30 centimetres from the camera and keep it still. Do not state any card details until the scanner reports them.';
      },
    });

    await ctx.connect(undefined, AutoSubscribe.SUBSCRIBE_ALL);
    probe?.snapshot('connected');
    // Chert prepares a room before a caller exists. Join promptly so Chert can
    // find us, but wait before opening a paid model session or sending video.
    const caller = await ctx.waitForParticipant();
    reader.syncTracks();

    // Video: one camera track, published by this agent. The Mac shows video only
    // from the participant that speaks, so the picture and the voice stay together.
    const source = new VideoSource(WIDTH, HEIGHT);
    const track = LocalVideoTrack.createVideoTrack('agent-video', source);
    let tick = 0, speaking = false;
    const frame = () => source.captureFrame(new VideoFrame(drawFace(tick++, speaking), WIDTH, HEIGHT, VideoBufferType.RGBA));
    frame();
    await ctx.room.localParticipant.publishTrack(track, { source: TrackSource.SOURCE_CAMERA });
    const timer = setInterval(frame, 1000 / FPS);

    session = new voice.AgentSession({
      llm: new realtime.RealtimeModel({ model: liveModel, voice: 'Puck' }),
    });
    session.on(voice.AgentSessionEventTypes.AgentStateChanged, event => { speaking = event.newState === 'speaking'; });
    ctx.addShutdownCallback(async () => {
      clearInterval(timer);
      await session.close();
      await track.close();
      await source.close();
    });
    await session.start({
      room: ctx.room,
      agent: new voice.Agent({ instructions, tools: { scan_staff_card: scanTool } }),
      record: false,
      inputOptions: {
        participantIdentity: caller.identity,
        audioEnabled: true,
        videoEnabled: false, // the Gemini Live plugin ignores video; cards go through the card reader
        textEnabled: false,
        closeOnDisconnect: true,
        deleteRoomOnClose: false,
      },
      outputOptions: { audioEnabled: true, transcriptionEnabled: false },
    });
    // Greet once when chert dev reports that FaceTime is ready (bridge_ready in the
    // room metadata): the Mac ends the call if it hears nothing within 10 seconds
    // of that. Participant arrival alone is not readiness, so there is no greeting
    // on it. Without the signal there is no greeting, and the caller speaks first.
    // The card scan ("awaiting card") starts with the greeting; the first voice
    // nudge comes no earlier than 9 s later, so it does not cut the greeting off.
    const stopGreeting = greetOnBridgeReady(ctx.room, () => {
      session.generateReply({ instructions: greeting });
      reader.scan();
    });
    ctx.addShutdownCallback(async () => stopGreeting());
  },
});

export default agent;

// Your own LiveKit project when .env.local has its three LIVEKIT_ values; otherwise Chert rooms through chert dev.
runAgent(agent, { file: fileURLToPath(import.meta.url), agentName: process.env.AGENT_NAME || 'video-agent' });
