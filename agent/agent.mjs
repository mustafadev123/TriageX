import { fileURLToPath } from 'node:url';
import { AutoSubscribe, defineAgent, voice } from '@livekit/agents';
import { realtime } from '@livekit/agents-plugin-google';
import { LocalVideoTrack, TrackSource, VideoBufferType, VideoFrame, VideoSource } from '@livekit/rtc-node';
import { drawFace, FPS, HEIGHT, WIDTH } from './face.mjs';
import { runAgent } from './chert-rooms.mjs';
import { greetOnBridgeReady } from './greet.mjs';
import { startFrameProbe } from './frame-probe.mjs';

// Change these instructions to build your own agent. Keep replies conversational.
const instructions = `You are the TriageX DEMO staffing agent on a FaceTime video call, part of a
hackathon demo. Everything is synthetic: no real hospital, patient or staff data.
The caller sees a simple animated face while you talk.
TriageX coordinates emergency-department surge staffing for a simulated Atlanta hospital network.
Keep each reply to one or two short sentences. Be clear that you are an AI.
Never claim you have verified anyone or recorded a confirmation: that is not connected yet.`;
// Said once, as soon as FaceTime is ready. Keep it short: it is the first thing the caller hears.
const greeting = 'Greet the caller in one short sentence: say you are the TriageX DEMO staffing agent, an AI, and ask how you can help.';
// Gemini Live via the Gemini API (GOOGLE_API_KEY). Override with GEMINI_LIVE_MODEL in .env.local.
const liveModel = process.env.GEMINI_LIVE_MODEL || 'gemini-2.5-flash-native-audio-preview-12-2025';

const agent = defineAgent({
  entry: async ctx => {
    // Frame probe: register track handlers before connecting, and subscribe to video as well as
    // audio, so the caller's facetime-remote-video track (if Chert sends it) reaches this agent.
    const probe = process.env.FRAME_PROBE === '0' ? null : startFrameProbe(ctx.room);
    ctx.addShutdownCallback(async () => probe?.stop());
    await ctx.connect(undefined, probe ? AutoSubscribe.SUBSCRIBE_ALL : AutoSubscribe.AUDIO_ONLY);
    probe?.snapshot('connected');
    // Chert prepares a room before a caller exists. Join promptly so Chert can
    // find us, but wait before opening a paid model session or sending video.
    const caller = await ctx.waitForParticipant();

    // Video: one camera track, published by this agent. The Mac shows video only
    // from the participant that speaks, so the picture and the voice stay together.
    const source = new VideoSource(WIDTH, HEIGHT);
    const track = LocalVideoTrack.createVideoTrack('agent-video', source);
    let tick = 0, speaking = false;
    const frame = () => source.captureFrame(new VideoFrame(drawFace(tick++, speaking), WIDTH, HEIGHT, VideoBufferType.RGBA));
    frame();
    await ctx.room.localParticipant.publishTrack(track, { source: TrackSource.SOURCE_CAMERA });
    const timer = setInterval(frame, 1000 / FPS);

    const session = new voice.AgentSession({
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
      agent: new voice.Agent({ instructions }),
      record: false,
      inputOptions: {
        participantIdentity: caller.identity,
        audioEnabled: true,
        videoEnabled: false,
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
    const stopGreeting = greetOnBridgeReady(ctx.room, () => session.generateReply({ instructions: greeting }));
    ctx.addShutdownCallback(async () => stopGreeting());
  },
});

export default agent;

// Your own LiveKit project when .env.local has its three LIVEKIT_ values; otherwise Chert rooms through chert dev.
runAgent(agent, { file: fileURLToPath(import.meta.url), agentName: process.env.AGENT_NAME || 'video-agent' });
